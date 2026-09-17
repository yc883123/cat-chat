// ============================================================
// 08-conversations.js —— 拆分自 public/app.js 第 2330-3114 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, escapeHtml, state, toast } from "./01-core.js";
import { renderMessages, revealMessage } from "./04-messages.js";
import { activeTaskStatuses, loadTasks, renderPermissionModeSwitch, taskDisplayTitle, taskKindLabel, taskStatusLabel } from "./06-tasks-plans.js";
import { applyConversationAgent, applyConversationModel, composerModelChoice } from "./07-models-agents.js";
import { readAsDataUrl } from "./10-upload.js";
import { detachRunSubscription, resumeConversationRun } from "./11-run-stream.js";
import { closePermissionModeMenu, closeQuickMessagePanel, hideChoiceButtons, updateDeepReasoningButton } from "./12-chat-input.js";
import { prefillPresetSkillsInComposer } from "./13-skill-refs.js";
import { clearFileRefCache, hideFilePopup } from "./16-file-refs.js";
import { closeFilePanel, closeSidebar } from "./14-file-panel.js";
export async function loadConversations() {
  const result = await api('/api/conversations');
  state.conversations = result.conversations;
  renderSidebar();
  if (!state.conversationId && state.conversations.length) {
    await openConversation(state.conversations[0].id);
  } else if (!state.conversations.length) {
    state.firstTurnInfo = null;
    renderMessages([]);
    renderPermissionModeSwitch();
  }
  renderComposerWorkspace();
}

export function formatRelativeTime(ts) {
  if (!ts) return '';
  const d = new Date(String(ts).replace(' ', 'T'));
  if (isNaN(d.getTime())) return '';
  const diff = Date.now() - d.getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min}分钟`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}小时`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}天`;
  const week = Math.floor(day / 7);
  if (week < 5) return `${week}周`;
  const month = Math.floor(day / 30);
  if (month < 12) return `${month}月`;
  return `${Math.floor(day / 365)}年`;
}

export function currentConversationWorkspaceGroup() {
  const c = state.conversations.find((x) => x.id === state.conversationId);
  return c ? (c.workspace_group || '').trim() : '';
}

// ---- 侧栏虚拟化（懒加载）：只渲染可视范围内的行，滚动时按窗口重绘 ----
export let sidebarRowCache = [];
export let sidebarOffsetCache = [];
export let sidebarTotalH = 0;
export let sidebarMetrics = null;
export let sidebarScrollToActive = false;
export let sidebarScrollRaf = 0;

// ESM 下 import 绑定只读：跨文件写入经 setter（读点保持直接引用不变）。
export function setSidebarScrollToActive(value) { sidebarScrollToActive = value; }
export function setSidebarScrollRaf(value) { sidebarScrollRaf = value; }
export let sidebarShowAll = new Set(); // 已“展开全部会话”的工作区名集合（默认全部折叠到 5 条）
export const SIDE_BUFFER = 240; // 视口上下预渲染缓冲（px）
export const SIDE_CONV_LIMIT = 5; // 每个展开工作区默认显示的最新会话数
// 「已收藏」是跨工作区的特殊分组（放在侧栏最下方，与工作区分组互不干扰）。
export const SIDE_FAVORITES_GROUP = '__favorites__';

export function sidebarMetricsNow() {
  if (sidebarMetrics) return sidebarMetrics;
  const holder = document.createElement('div');
  holder.style.cssText = 'position:fixed;left:-9999px;top:0;visibility:hidden;width:260px;';
  holder.innerHTML = '<div class="workspace-group"><div class="workspace-group-header">X</div></div>'
    + '<button class="workspace-new-chat">＋</button><div class="conversation-item"><span>X</span></div>'
    + '<button class="workspace-showmore">展开其余 0 个会话</button>';
  document.body.appendChild(holder);
  sidebarMetrics = {
    header: holder.querySelector('.workspace-group-header').offsetHeight || 32,
    newchat: holder.querySelector('.workspace-new-chat').offsetHeight || 34,
    item: holder.querySelector('.conversation-item').offsetHeight || 40,
    showmore: holder.querySelector('.workspace-showmore').offsetHeight || 34,
  };
  holder.remove();
  return sidebarMetrics;
}

export function sidebarRowHeight(row) {
  const m = sidebarMetricsNow();
  if (row.type === 'header') return m.header;
  if (row.type === 'newchat') return m.newchat;
  if (row.type === 'showmore' || row.type === 'showless') return m.showmore;
  return m.item;
}

export function computeSidebarOffsets(rows) {
  const offsets = new Array(rows.length);
  let y = 0;
  for (let i = 0; i < rows.length; i++) { offsets[i] = y; y += sidebarRowHeight(rows[i]); }
  return { offsets, totalH: y };
}

export function sidebarRowAt(offsets, pos) {
  let lo = 0, hi = offsets.length - 1, ans = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (offsets[mid] <= pos) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

export function sidebarRowHtml(row) {
  if (row.type === 'header') {
    return `<div class="workspace-group ${row.isExp ? 'expanded' : ''}" data-workspace-name="${escapeHtml(row.wsName)}" data-workspace-dir="${escapeHtml(row.dir)}">
      <div class="workspace-group-header" data-action="toggle-group">
        <span class="workspace-caret">▸</span>
        <span class="workspace-group-name">${escapeHtml(row.label)}</span>
        <span class="workspace-count">${row.count}</span>
        ${row.isUngrouped || row.isFavorites ? '' : `<button class="workspace-delete" data-action="delete-workspace" data-workspace-name="${escapeHtml(row.wsName)}" title="删除工作区" aria-label="删除工作区">×</button>`}
      </div>
    </div>`;
  }
  if (row.type === 'newchat') {
    return `<button class="workspace-new-chat" data-action="new-in-group" data-workspace-group="${escapeHtml(row.wsName)}" data-workspace-dir="${escapeHtml(row.dir)}">＋ 新会话</button>`;
  }
  if (row.type === 'showmore') {
    return `<button class="workspace-showmore" data-action="show-more" data-workspace-name="${escapeHtml(row.wsName)}">展开其余 ${row.remaining} 个会话</button>`;
  }
  if (row.type === 'showless') {
    return `<button class="workspace-showmore" data-action="show-less" data-workspace-name="${escapeHtml(row.wsName)}">收起</button>`;
  }
  const c = row.c;
  const favorite = Number(c.favorite || 0) === 1;
  // 分支徽标 / 源计数：都以列表数据里的字段为唯一依据（branched_from_id / branch_count /
  // branch_source_title 随会话列表一次带下），点开分支链面板就是唯一的额外请求。
  const branchSourceId = String(c.branched_from_id || '');
  const branchCount = Number(c.branch_count || 0) || 0;
  const branchSourceTitle = String(c.branch_source_title || '');
  const branchBadge = branchSourceId
    ? `<button class="conversation-branch" data-action="open-branch-chain" title="${escapeHtml(branchSourceTitle ? `分支自《${branchSourceTitle}》` : '源会话已删除')}" aria-label="查看分支链" aria-haspopup="menu"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6.5" cy="5.5" r="2.4"></circle><circle cx="6.5" cy="18.5" r="2.4"></circle><circle cx="17.5" cy="12" r="2.4"></circle><path d="M6.5 7.9v8.2M8.9 5.5h3.1a2.5 2.5 0 0 1 2.5 2.5v1.6M8.9 18.5h3.1a2.5 2.5 0 0 0 2.5-2.5v-1.6"></path></svg></button>`
    : '';
  const branchCountBadge = branchCount > 0
    ? `<button class="conversation-branch-count" data-action="open-branch-chain" title="这个会话有 ${branchCount} 个分支" aria-label="查看 ${branchCount} 个分支" aria-haspopup="menu">⑂${branchCount}</button>`
    : '';
  // data-group：虚拟列表里行是扁平的（工作区分组只包住表头），带上所属分组便于
  // 「已收藏」这类特殊分组的定位/断言（不参与任何业务逻辑）。
  return `<div class="conversation-item ${c.id === state.conversationId ? 'active' : ''}" data-conversation-id="${c.id}" data-group="${escapeHtml(row.wsName || '')}">
    <button class="conversation-star ${favorite ? 'is-favorite' : ''}" data-action="toggle-favorite" title="${favorite ? '取消收藏' : '收藏会话'}" aria-label="${escapeHtml(c.title)} ${favorite ? '取消收藏' : '收藏'}" aria-pressed="${favorite}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.6l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.8l5.9-.9z"></path></svg></button>
    ${branchBadge}
    <button class="conversation-open" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</button>
    ${branchCountBadge}
    <span class="conversation-time">${escapeHtml(formatRelativeTime(c.updated_at))}</span>
    <button class="conversation-more" data-action="open-conversation-menu" title="更多操作" aria-label="${escapeHtml(c.title)} 的更多操作" aria-haspopup="menu">⋯</button>
  </div>`;
}

// 已渲染的窗口区间（行索引 + 总高度）。滚动时若区间没变就**不重写 DOM**：
// 每帧 innerHTML 重写要重新解析/布局整棵子树，是"滚轮发滞"的主要来源；
// 行高固定，视口内滚过一行才需要换窗口（约每 34px 一次）。
let sidebarWindowRange = { start: -1, end: -1, totalH: 0, rendered: false };

export function resetSidebarWindowRange() {
  sidebarWindowRange = { start: -1, end: -1, totalH: 0, rendered: false };
}

// 侧栏当前是否在展示「全文搜索结果」。结果条数固定（≤50）且不是虚拟列表，因此凡是
// 「按行窗口重写 #sidebarWorkspaceTree」的路径都必须让路，否则一次滚动委托就会把结果盖掉。
export function sidebarShowsSearchResults() {
  return state.workspaceSearchMode === 'full' && Boolean((state.workspaceSearch || '').trim());
}

export function renderSidebarWindow(targetScrollTop, { force = false } = {}) {
  const tree = $('#sidebarWorkspaceTree');
  if (!tree) return;
  if (sidebarShowsSearchResults()) return;
  if (!sidebarRowCache.length) {
    // 顶栏「新会话」按钮已移除（每个工作区分组自带「＋ 新会话」）；这里保留一个兜底入口，
    // 否则"一条会话都没有"时侧栏没有任何新建入口。
    tree.innerHTML = '<div class="workspace-empty">暂无对话<button class="workspace-new-chat" data-action="new-chat" type="button">＋ 新建会话</button></div>';
    resetSidebarWindowRange();
    return;
  }
  const vh = tree.clientHeight || Math.max(240, Math.round(window.innerHeight * 0.4));
  // 关键修复：窗口必须按「夹紧后的真实滚动位置」渲染。
  // 会话列表展开/收起、点击末尾条目触发跳转时，旧的 scrollTop 可能超出新的
  // 可滚动范围（列表比视口矮或比之前短）。此前用未夹紧的值去算窗口，只渲染出
  // 末尾几行，上半部分全空白——表现为“点击末尾条目后上面的条目不显示”。
  //
  // 上限必须取浏览器真实 scrollHeight（含 .conversation-list 的上下 padding）：
  // 此前用 sidebarTotalH - vh 会少算 padding（实测 16px），每次滚动都被回写成
  // 偏小的值——表现为滚轮"滚不动/越滚越慢"，列表底部 16px 永远到不了。
  const maxScroll = Math.max(0, tree.scrollHeight - vh);
  const st = Math.max(0, Math.min(Number(targetScrollTop) || 0, maxScroll));
  let start = Math.max(0, sidebarRowAt(sidebarOffsetCache, st - SIDE_BUFFER));
  let end = sidebarRowAt(sidebarOffsetCache, st + vh + SIDE_BUFFER) + 1;
  if (end <= start) end = start + 1;
  end = Math.min(sidebarRowCache.length, end);
  const unchanged = sidebarWindowRange.rendered
    && sidebarWindowRange.start === start
    && sidebarWindowRange.end === end
    && sidebarWindowRange.totalH === sidebarTotalH;
  if (!force && unchanged) {
    // 窗口没变：只保证滚动位置与夹紧值一致，不动 DOM。
    if (tree.scrollTop !== st) tree.scrollTop = st;
    return;
  }
  const html = sidebarRowCache.slice(start, end).map(sidebarRowHtml).join('');
  tree.innerHTML = `<div class="sidebar-virtual" style="height:${sidebarTotalH}px">`
    + `<div class="sidebar-virtual-window" style="top:${sidebarOffsetCache[start]}px">${html}</div></div>`;
  sidebarWindowRange = { start, end, totalH: sidebarTotalH, rendered: true };
  // 高度突变后浏览器可能自行钳位 scrollTop；把夹紧后的值再写回一次，保证窗口与滚动一致。
  if (tree.scrollTop !== st) tree.scrollTop = st;
}

export function sidebarClampWidth(w) {
  return Math.max(170, Math.min(Math.max(170, window.innerWidth * 0.3), w));
}

export function restoreSidebarWidth() {
  const saved = parseFloat(localStorage.getItem('naibaChatSidebarW') || '');
  const base = (saved && !isNaN(saved)) ? saved : 272;
  document.documentElement.style.setProperty('--sidebar-w', sidebarClampWidth(base) + 'px');
}

export function renderSidebar() {
  const tree = $('#sidebarWorkspaceTree');
  if (!tree) return;
  // 全文模式：结果列表**替换**会话树区域（同一个容器），命中词高亮、头部明示截断。
  // 关键词清空后 `sidebarShowsSearchResults()` 变假，自然退回下面的会话树渲染。
  if (sidebarShowsSearchResults()) {
    renderSearchResults(tree, (state.workspaceSearch || '').trim());
    return;
  }
  const search = (state.workspaceSearch || '').trim().toLowerCase();
  const activeWs = currentConversationWorkspaceGroup();
  if (!state.expandedGroups.has('__init')) {
    // 启动时只展开“当前会话所处的工作区”，其余工作区折叠；当前会话尚未确定时暂不展开任何组。
    // 「已收藏」默认展开：它本身就是用户主动挑出来的短列表。
    state.expandedGroups = new Set(['__init', SIDE_FAVORITES_GROUP]);
    if (state.conversations.some((c) => c.id === state.conversationId)) {
      state.expandedGroups.add(activeWs);
    }
  }
  const groups = new Map();
  for (const c of state.conversations) {
    const key = (c.workspace_group || '').trim();
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  const registered = state.workspaces || [];
  const orderedNames = [];
  const seen = new Set();
  for (const ws of registered) {
    if (!seen.has(ws.name)) { seen.add(ws.name); orderedNames.push(ws.name); }
  }
  for (const key of groups.keys()) {
    if (key && !seen.has(key)) { seen.add(key); orderedNames.push(key); }
  }
  orderedNames.push('');
  const sortConv = (list) => {
    const arr = [...list];
    if (state.workspaceSort === 'name') arr.sort((a, b) => String(a.title).localeCompare(String(b.title), 'zh'));
    else arr.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    return arr;
  };

  const rows = [];
  for (const wsName of orderedNames) {
    const isUngrouped = wsName === '';
    const label = isUngrouped ? '未分组' : wsName;
    const dir = (registered.find((w) => w.name === wsName) || {}).dir || '';
    let list = sortConv(groups.get(wsName) || []);
    if (search) {
      const filtered = list.filter((c) => String(c.title || '').toLowerCase().includes(search) || label.toLowerCase().includes(search));
      if (!filtered.length) continue;
      list = filtered;
    }
    const isExp = state.expandedGroups.has(wsName);
    rows.push({ type: 'header', wsName, label, dir, isUngrouped, isExp, count: list.length });
    if (isExp) {
      rows.push({ type: 'newchat', wsName, dir });
      const showAll = sidebarShowAll.has(wsName);
      const limit = SIDE_CONV_LIMIT;
      const shown = showAll || list.length <= limit ? list : list.slice(0, limit);
      for (const c of shown) rows.push({ type: 'item', c, wsName });
      if (list.length > limit && !showAll) {
        rows.push({ type: 'showmore', wsName, remaining: list.length - limit });
      } else if (list.length > limit && showAll) {
        rows.push({ type: 'showless', wsName });
      }
    }
  }

  // 「已收藏」分组固定在侧栏最下方：跨工作区汇总，不受工作区分组的折叠/5 条上限影响，
  // 也不把会话从原工作区移走（两处都显示，避免用户以为会话丢了）。
  const favoriteList = sortConv(state.conversations.filter((c) => Number(c.favorite || 0) === 1))
    .filter((c) => !search || String(c.title || '').toLowerCase().includes(search));
  if (favoriteList.length) {
    const isExp = state.expandedGroups.has(SIDE_FAVORITES_GROUP);
    rows.push({
      type: 'header', wsName: SIDE_FAVORITES_GROUP, label: '已收藏', dir: '',
      isUngrouped: false, isFavorites: true, isExp, count: favoriteList.length,
    });
    if (isExp) {
      for (const c of favoriteList) rows.push({ type: 'item', c, wsName: SIDE_FAVORITES_GROUP });
    }
  }

  const { offsets, totalH } = computeSidebarOffsets(rows);
  sidebarRowCache = rows; sidebarOffsetCache = offsets; sidebarTotalH = totalH;
  let st = tree.scrollTop;
  if (sidebarScrollToActive) {
    sidebarScrollToActive = false;
    st = sidebarScrollForActive(rows, offsets, st, tree.clientHeight || 0);
  }
  // renderSidebarWindow 内部会按「夹紧后的真实滚动位置」切窗口并回写 scrollTop，
  // 不再在窗口算完后单独赋值——避免高度突变时窗口与滚动状态错位。
  // force：行缓存刚重建，即使区间索引相同也必须重绘（内容可能已变）。
  renderSidebarWindow(st, { force: true });
}

// ---- 侧栏「全文」搜索：结果替换会话树区域渲染 ----
// 数据来自 GET /api/search/messages（只读接口，后端 instr(lower()) 子串匹配）。
// 竞态处理：请求不取消（切工作区/会话都可能触发），但**渲染前**一律校验「当前仍是全文模式
// 且关键词与响应一致」，过期响应直接丢弃——否则快速输入时会把上一个词的命中画进来。
export const SEARCH_DEBOUNCE_MS = 300;
const SEARCH_HIT_LIMIT = 30;

// 命中词高亮：snippet 是原文的连续子串，所以这里只在它自己内部找关键词。
// 先转义再插 <mark>——顺序反了会让转义后的 &amp; 之类的长度与原文错位。
export function highlightSnippet(snippet, needle) {
  const text = String(snippet || '');
  const target = String(needle || '');
  if (!target) return escapeHtml(text);
  const lower = text.toLowerCase();
  const wanted = target.toLowerCase();
  let out = '';
  let at = 0;
  for (;;) {
    const found = lower.indexOf(wanted, at);
    if (found < 0) break;
    out += escapeHtml(text.slice(at, found))
      + `<mark>${escapeHtml(text.slice(found, found + wanted.length))}</mark>`;
    at = found + wanted.length;
  }
  return out + escapeHtml(text.slice(at));
}

export function renderSearchResults(tree, query) {
  const result = state.searchResults;
  if (!result || state.searchQuery !== query) {
    tree.innerHTML = `<div class="search-empty">正在检索「${escapeHtml(query)}」…</div>`;
    return;
  }
  if (result.error) {
    tree.innerHTML = `<div class="search-empty">搜索失败：${escapeHtml(String(result.error))}</div>`;
    return;
  }
  const hits = Array.isArray(result.hits) ? result.hits : [];
  if (!hits.length) {
    tree.innerHTML = `<div class="search-empty">没有找到包含「${escapeHtml(query)}」的消息</div>`;
    return;
  }
  const head = `<div class="search-results-head"><b>共 ${Number(result.total_hits || 0)} 条命中</b>`
    + (result.truncated ? `<span>（只显示最近 ${hits.length} 条）</span>` : '')
    + '</div>';
  const items = hits.map((hit) => (
    `<button type="button" class="search-hit${hit.conversation_id === state.conversationId ? ' active' : ''}"`
    + ` data-search-hit="${escapeHtml(hit.message_id || '')}"`
    + ` data-conversation-id="${escapeHtml(hit.conversation_id || '')}"`
    + ' title="跳到这条消息">'
    + '<span class="search-hit-row">'
    + `<span class="search-hit-title">${escapeHtml(hit.conversation_title || '（无标题）')}</span>`
    + (hit.in_context === false ? '<span class="search-hit-out">已划出上下文</span>' : '')
    + `<span class="search-hit-time">${escapeHtml(formatRelativeTime(hit.created_at))}</span>`
    + '</span>'
    + `<span class="search-hit-snippet">${highlightSnippet(hit.snippet, state.searchQuery)}</span>`
    + '</button>'
  )).join('');
  tree.innerHTML = `<div class="search-results">${head}${items}</div>`;
}

export async function runFullTextSearch() {
  const query = (state.workspaceSearch || '').trim();
  state.searchResults = null;
  state.searchQuery = '';
  state.searchLoading = false;
  if (!query) {
    renderSidebar();
    return;
  }
  state.searchQuery = query;
  state.searchLoading = true;
  renderSidebar();
  try {
    const result = await api(`/api/search/messages?q=${encodeURIComponent(query)}&limit=${SEARCH_HIT_LIMIT}`);
    if (!sidebarShowsSearchResults() || state.searchQuery !== query) return;  // 过期响应：丢弃
    state.searchResults = result;
  } catch (error) {
    if (!sidebarShowsSearchResults() || state.searchQuery !== query) return;
    state.searchResults = { hits: [], total_hits: 0, truncated: false, error: error.message };
  }
  state.searchLoading = false;
  renderSidebar();
}

// 模式切换（标题｜全文）：默认标题模式，行为与升级前逐字节一致（本地过滤，不发请求）。
export function setWorkspaceSearchMode(mode) {
  const next = mode === 'full' ? 'full' : 'title';
  state.workspaceSearchMode = next;
  state.searchResults = null;
  state.searchQuery = '';
  state.searchLoading = false;
  syncSearchModeUi();
  renderSidebar();
  if (next === 'full' && (state.workspaceSearch || '').trim()) void runFullTextSearch();
}

export function syncSearchModeUi() {
  const full = state.workspaceSearchMode === 'full';
  const titleButton = $('#workspaceSearchModeTitle');
  const fullButton = $('#workspaceSearchModeFull');
  titleButton?.classList.toggle('active', !full);
  fullButton?.classList.toggle('active', full);
  titleButton?.setAttribute('aria-pressed', String(!full));
  fullButton?.setAttribute('aria-pressed', String(full));
  const input = $('#workspaceSearchInput');
  if (input) input.placeholder = full ? '搜索消息正文' : '搜索对话';
}

// 命中项跳转：先切会话，再按 message_id 定位并高亮（定位算法与刻度轨跳转同口径）。
async function openSearchHit(conversationId, messageId) {
  if (!conversationId || !messageId) return;
  try {
    if (conversationId !== state.conversationId) await openConversation(conversationId);
    else renderSidebar();   // 同一会话：只把「当前会话」的命中标记刷新一下
    const located = revealMessage(messageId);
    if (!located) toast('这条消息已不在该对话中（可能刚被删除）');
    else closeSidebar();    // 手机端顺手收起抽屉；桌面端本就是空操作
  } catch (error) {
    toast(`打开会话失败：${error.message}`);
  }
}

// 当前会话行的滚动定位：**最小滚动**——已可见就一动不动；不可见才把最近的那一份
// （同一会话可能同时出现在工作区分组与「已收藏」分组）刚好带进视口，绝不强制顶到最上。
// 此前一律 `st = offsets[idx]`，点一下列表就整片滚到顶，用户根本找不回原来的位置。
export function sidebarScrollForActive(rows, offsets, currentScrollTop, viewHeight) {
  const indexes = [];
  rows.forEach((row, index) => {
    if (row.type === 'item' && row.c.id === state.conversationId) indexes.push(index);
  });
  if (!indexes.length) return currentScrollTop;
  const viewTop = Math.max(0, Number(currentScrollTop) || 0);
  const viewBottom = viewTop + viewHeight;
  const span = (index) => {
    const top = offsets[index];
    return { top, bottom: top + sidebarRowHeight(rows[index]) };
  };
  if (indexes.some((index) => {
    const { top, bottom } = span(index);
    return bottom > viewTop && top < viewBottom;
  })) {
    return viewTop; // 已可见：保持用户当前的位置
  }
  let best = indexes[0];
  let bestDistance = Infinity;
  for (const index of indexes) {
    const { top, bottom } = span(index);
    const distance = top > viewBottom ? top - viewBottom : (bottom < viewTop ? viewTop - bottom : 0);
    if (distance < bestDistance) { bestDistance = distance; best = index; }
  }
  const { top, bottom } = span(best);
  if (bottom > viewBottom) return bottom - viewHeight;
  if (top < viewTop) return top;
  return viewTop;
}

export function renderComposerWorkspace() {
  const select = $('#composerWorkspaceSelect');
  if (!select) return;
  const current = state.conversations.find((c) => c.id === state.conversationId);
  const currentGroup = current ? (current.workspace_group || '').trim() : '';
  const options = ['', ...(state.workspaces || []).map((w) => w.name)];
  select.innerHTML = options.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name || '未分组')}</option>`).join('');
  select.value = currentGroup;
}

export async function onComposerWorkspaceChange(event) {
  const id = state.conversationId;
  if (!id) return;
  const group = event.target.value || '';
  try {
    const updated = await api(`/api/conversations/${id}/settings`, { method: 'POST', body: { workspace_group: group } });
    const index = state.conversations.findIndex((c) => c.id === id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    if (updated.workspace_dir) state.workspaceDir = updated.workspace_dir;
    // 工作区换了：@ 引用弹层的目录缓存必须失效（相对路径相同但根不同）
    hideFilePopup();
    clearFileRefCache();
    renderSidebar();
    renderComposerWorkspace();
    toast(group ? `已切换到工作区「${group}」` : '已移至未分组');
  } catch (error) {
    toast(`切换工作区失败：${error.message}`);
    renderComposerWorkspace();
  }
}

export async function onSidebarTreeClick(event) {
  // 全文搜索命中项：先切会话，再按 message_id 定位并高亮。放在最前面——它不是
  // .conversation-item，落到下面的分支会被无声忽略。
  const hit = event.target.closest('[data-search-hit]');
  if (hit) {
    await openSearchHit(hit.dataset.conversationId || '', hit.dataset.searchHit || '');
    return;
  }
  const actionEl = event.target.closest('[data-action]');
  if (actionEl) {
    const action = actionEl.dataset.action;
    const groupEl = actionEl.closest('.workspace-group');
    if (action === 'toggle-group') {
      const name = groupEl?.dataset.workspaceName || '';
      if (state.expandedGroups.has(name)) state.expandedGroups.delete(name);
      else state.expandedGroups.add(name);
      renderSidebar();
    } else if (action === 'show-more') {
      const name = actionEl.dataset.workspaceName || '';
      sidebarShowAll.add(name);
      renderSidebar();
    } else if (action === 'show-less') {
      const name = actionEl.dataset.workspaceName || '';
      sidebarShowAll.delete(name);
      renderSidebar();
    } else if (action === 'new-in-group') {
      createConversation(actionEl.dataset.workspaceGroup || '', actionEl.dataset.workspaceDir || '', true);
    } else if (action === 'new-chat') {
      createConversation('', '', true);
    } else if (action === 'delete-workspace') {
      deleteWorkspace(actionEl.dataset.workspaceName || '');
    } else if (action === 'toggle-favorite') {
      const id = actionEl.closest('.conversation-item')?.dataset.conversationId || '';
      toggleConversationFavorite(id);
    } else if (action === 'open-branch-chain') {
      const id = actionEl.closest('.conversation-item')?.dataset.conversationId || '';
      void openBranchChainPanel(actionEl, id);
    } else if (action === 'open-conversation-menu') {
      const id = actionEl.closest('.conversation-item')?.dataset.conversationId || '';
      openConversationMenu(actionEl, id);
    }
    return;
  }
  const item = event.target.closest('.conversation-item');
  if (!item) return;
  // 整条都可点（含上下边缘、左侧留白、按钮之间的空隙）：此前只有中间的文字按钮能命中，
  // 鼠标落在条目上下边缘时既不变手型也不切换会话，判定区与视觉区不一致。
  // 五角星与「⋯」在上面的 [data-action] 分支已处理并 return。
  // 服务端不可用时只提示（状态点会转红），不要让未处理的 Promise 拒绝弹出调试条。
  openConversation(item.dataset.conversationId)
    .catch((error) => toast(`打开会话失败：${error.message}`));
}

// ---- 会话条目「⋯」菜单 / 收藏 / 重命名 ----
// 菜单挂在 body 上（fixed 定位）：侧栏是 overflow 滚动容器，内嵌菜单会被裁剪（教训 §九.27）。
let conversationMenuId = '';

export function closeConversationMenu() {
  const menu = $('#conversationItemMenu');
  conversationMenuId = '';
  if (!menu || menu.hidden) return;
  menu.hidden = true;
}

export function conversationMenuTargetId() {
  return conversationMenuId;
}

export function openConversationMenu(anchorEl, id) {
  const menu = $('#conversationItemMenu');
  if (!menu || !id) return;
  if (!menu.hidden && conversationMenuId === id) { closeConversationMenu(); return; }
  conversationMenuId = id;
  if (menu.parentElement !== document.body) document.body.appendChild(menu);
  menu.hidden = false;
  const rect = anchorEl.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  const edge = 8;
  const left = Math.min(
    Math.max(edge, rect.right - menuRect.width),
    Math.max(edge, window.innerWidth - menuRect.width - edge),
  );
  let top = rect.bottom + 4;
  if (top + menuRect.height > window.innerHeight - edge) {
    top = Math.max(edge, rect.top - menuRect.height - 4);
  }
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;
}

// ---- 分支链面板：点会话行的 ⑂ 徽标 / ⑂N 计数弹出 ----
// 只解决「认得出、跳得过去」：源会话 + 兄弟分支（自己是分支时），或自己的全部分支
// （自己是源时）。侧栏本身仍是平铺 + 徽标，不画树——树与 updated_at 排序根本冲突，
// 多级分支还会打乱虚拟列表的行索引。
let branchChainConversationId = '';

export function closeBranchChainPanel() {
  const panel = $('#branchChainPanel');
  branchChainConversationId = '';
  if (!panel || panel.hidden) return;
  panel.hidden = true;
}

export function branchChainTargetId() {
  return branchChainConversationId;
}

// 定位口径与会话「⋯」菜单一致（§九.27）：fixed 挂 body，锚点右下角为起点、贴边时上翻。
export function positionBranchChainPanel(anchorEl) {
  const panel = $('#branchChainPanel');
  if (!panel || panel.hidden) return;
  const rect = anchorEl.getBoundingClientRect();
  const size = panel.getBoundingClientRect();
  const edge = 8;
  const left = Math.min(
    Math.max(edge, rect.left),
    Math.max(edge, window.innerWidth - size.width - edge),
  );
  let top = rect.bottom + 4;
  if (top + size.height > window.innerHeight - edge) {
    top = Math.max(edge, rect.top - size.height - 4);
  }
  panel.style.left = `${Math.round(left)}px`;
  panel.style.top = `${Math.round(top)}px`;
}

export function renderBranchChain(chain) {
  const list = $('#branchChainList');
  const head = $('#branchChainHeadText');
  if (!list) return;
  const items = Array.isArray(chain?.items) ? chain.items : [];
  const role = String(chain?.role || 'none');
  if (head) head.textContent = role === 'branch' ? '分支链（源 + 兄弟）' : '这个会话的分支';
  if (role === 'none' || !items.length) {
    list.innerHTML = '<div class="branch-chain-empty">这个会话没有分支</div>';
    return;
  }
  const rows = [];
  if (chain.source_deleted) rows.push('<div class="branch-chain-item is-gone">源会话已删除</div>');
  for (const item of items) {
    rows.push(
      `<button type="button" class="branch-chain-item${item.is_current ? ' is-current' : ''}${item.is_source ? ' is-source' : ''}"`
      + ` data-branch-goto="${escapeHtml(item.id || '')}" title="${escapeHtml(item.title || '')}">`
      + `${escapeHtml(item.title || '（无标题）')}</button>`
    );
  }
  list.innerHTML = rows.join('');
}

export async function openBranchChainPanel(anchorEl, conversationId) {
  const panel = $('#branchChainPanel');
  const list = $('#branchChainList');
  if (!panel || !list || !conversationId) return;
  if (!panel.hidden && branchChainConversationId === conversationId) {
    closeBranchChainPanel();
    return;
  }
  branchChainConversationId = conversationId;
  if (panel.parentElement !== document.body) document.body.appendChild(panel);
  list.innerHTML = '<div class="branch-chain-empty">正在读取分支链…</div>';
  panel.hidden = false;
  positionBranchChainPanel(anchorEl);
  try {
    const chain = await api(`/api/conversations/${conversationId}/branch_chain`);
    if (panel.hidden || branchChainConversationId !== conversationId) return;  // 期间被关掉/换了锚点
    renderBranchChain(chain);
  } catch (error) {
    if (panel.hidden || branchChainConversationId !== conversationId) return;
    list.innerHTML = `<div class="branch-chain-empty">读取失败：${escapeHtml(error.message)}</div>`;
  }
  positionBranchChainPanel(anchorEl);   // 内容换过高度，重新夹一次视口
}

export async function toggleConversationFavorite(id) {
  const conversation = state.conversations.find((item) => item.id === id);
  if (!conversation) return;
  const next = Number(conversation.favorite || 0) !== 1;
  closeConversationMenu();
  // 乐观更新：收藏只是侧栏归类标记，失败时回滚并提示。
  conversation.favorite = next ? 1 : 0;
  renderSidebar();
  try {
    const updated = await api(`/api/conversations/${id}/settings`, {
      method: 'POST',
      body: { favorite: next },
    });
    const index = state.conversations.findIndex((item) => item.id === id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    renderSidebar();
    toast(next ? '已收藏该会话' : '已取消收藏');
  } catch (error) {
    conversation.favorite = next ? 0 : 1;
    renderSidebar();
    toast(`收藏失败：${error.message}`);
  }
}

export function openRenameConversation(id) {
  const conversation = state.conversations.find((item) => item.id === id);
  if (!conversation) return;
  closeConversationMenu();
  state.renameConversationId = id;
  $('#renameConversationHint').textContent = conversation.title || '当前对话';
  // 与旧「对话设置」一致：自动命名的标题不回填（留空 = 恢复自动命名）。
  $('#renameConversationInput').value = conversation.title_customized ? (conversation.title || '') : '';
  $('#renameConversationDialog').showModal();
  $('#renameConversationInput').focus();
  $('#renameConversationInput').select();
}

export async function saveRenameConversation(event) {
  event.preventDefault();
  const id = state.renameConversationId;
  if (!id) return;
  const button = $('#saveRenameConversation');
  if (button) button.disabled = true;
  try {
    const updated = await api(`/api/conversations/${id}/settings`, {
      method: 'POST',
      body: { title: $('#renameConversationInput').value },
    });
    const index = state.conversations.findIndex((item) => item.id === id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    $('#renameConversationDialog').close();
    state.renameConversationId = '';
    renderSidebar();
    toast('对话已重命名');
  } catch (error) {
    toast(`重命名失败：${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

export async function pick_workspace_directory(initial = '') {
  try {
    return await api('/api/workspace/pick', { method: 'POST', body: { initial } });
  } catch (error) {
    toast(`目录选择失败：${error.message}`);
    return { cancelled: true };
  }
}

// 新建工作区：先选目录，再在对话框里填名称（不用 window.prompt——pywebview 下不可靠）。
export async function createWorkspace() {
  const result = await pick_workspace_directory();
  if (!result || result.cancelled || !result.path) return;
  const dir = result.resolved || result.path;
  state.newWorkspaceDir = dir;
  $('#newWorkspaceDirHint').textContent = dir;
  $('#newWorkspaceName').value = String(dir.split(/[\\/]/).filter(Boolean).pop() || '新工作区');
  $('#newWorkspaceDialog').showModal();
  $('#newWorkspaceName').focus();
  $('#newWorkspaceName').select();
}

export async function saveNewWorkspace(event) {
  event.preventDefault();
  const dir = String(state.newWorkspaceDir || '').trim();
  const name = String($('#newWorkspaceName').value || '').trim();
  if (!dir) {
    toast('请先选择工作区目录');
    $('#newWorkspaceDialog').close();
    return;
  }
  if (!name) {
    toast('工作区名称不能为空');
    $('#newWorkspaceName').focus();
    return;
  }
  const button = $('#saveNewWorkspace');
  if (button) button.disabled = true;
  try {
    const data = await api('/api/workspaces', { method: 'POST', body: { name, dir } });
    state.workspaces = data.workspaces || [];
    state.expandedGroups.add(name);
    state.newWorkspaceDir = '';
    $('#newWorkspaceDialog').close();
    toast(`已创建工作区「${name}」`);
    // 新建工作区后立即在该工作区内创建一个新对话并打开，选择框同步显示该工作区。
    try {
      await createConversation(name, dir, true);
    } catch (error) {
      toast(`工作区已创建，但新建对话失败：${error.message}`);
    }
  } catch (error) {
    toast(`创建工作区失败：${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

export async function deleteWorkspace(name) {
  if (!name) return;
  const count = state.conversations.filter((c) => (c.workspace_group || '').trim() === name).length;
  const hint = count ? `其下 ${count} 个对话将归档到「未分组」。` : '';
  if (!confirm(`确定删除工作区「${name}」？${hint}`)) return;
  try {
    const data = await api('/api/workspaces/delete', { method: 'POST', body: { name } });
    state.workspaces = data.workspaces || [];
    state.conversations.forEach((c) => {
      if ((c.workspace_group || '').trim() === name) c.workspace_group = '';
    });
    state.expandedGroups.delete(name);
    renderSidebar();
    renderComposerWorkspace();
    toast(`已删除工作区「${name}」`);
  } catch (error) {
    toast(`删除工作区失败：${error.message}`);
  }
}

export async function createConversation(workspaceGroup = '', workspaceDir = '', prefillSkills = false) {
  detachRunSubscription();
  hideChoiceButtons();
  const knownAgentIds = new Set((state.bootstrap?.agents || []).map((a) => String(a.id)));
  const prevAgentId = state.conversations.find((c) => String(c.id) === state.conversationId)?.agent_id;
  const defaultAgentId = String(state.bootstrap?.default_agent_id || '');
  // 新建会话默认沿用上一个会话使用的 Agent；若其已被删除则回退默认/首个，避免把失效 id 发给后端。
  const nextAgentId = String(
    (prevAgentId && knownAgentIds.has(String(prevAgentId)) && String(prevAgentId))
    || (knownAgentIds.has(defaultAgentId) && defaultAgentId)
    || (state.bootstrap?.agents?.[0]?.id) || ''
  );
  const conversation = await api('/api/conversations', {
    method: 'POST',
    body: {
      interaction_mode: 'craft',
      permission_mode: 'auto',
      web_search_enabled: false,
      deep_reasoning_enabled: false,
      agent_id: nextAgentId,
      model_key: $('#modelSelect')?.value || '',
      model_name: composerModelChoice(),
      // 新建对话继承当前全局工作区目录；若在某工作区内新建则覆盖为该工作区目录并绑定分组。
      workspace_dir: workspaceDir || state.bootstrap?.settings?.workspace_dir || '',
      workspace_group: workspaceGroup || '',
    },
  });
  state.conversationId = conversation.id;
  if (conversation.workspace_dir) {
    state.workspaceDir = conversation.workspace_dir;
  }
  // 新会话没有首轮上下文：清掉上一个会话残留的 firstTurnInfo，否则首轮上下文
  // 折叠卡会错误地出现在新会话页面（该卡数据契约上只属于 openConversation 装载的会话）。
  state.firstTurnInfo = null;
  state.conversations.unshift(conversation);
  state.expandedGroups.add(currentConversationWorkspaceGroup());
  renderComposerWorkspace();
  sidebarScrollToActive = true;
  renderSidebar();
  applyConversationModel(conversation);
  applyConversationAgent(conversation);
  state.webSearchEnabled = Boolean(Number(conversation.web_search_enabled || 0));
  state.deepReasoningEnabled = Boolean(Number(conversation.deep_reasoning_enabled || 0));
  state.reasoningEffort = conversation.reasoning_effort || (state.deepReasoningEnabled ? 'medium' : 'auto');
  updateDeepReasoningButton();
  renderMessages([]);
  renderPermissionModeSwitch();
  closeSidebar();
  if (prefillSkills) prefillPresetSkillsInComposer(conversation);
  $('#messageInput').focus();
}

export async function openConversation(id) {
  if (id !== state.conversationId) {
    detachRunSubscription();
    hideChoiceButtons();
    // 文件面板属于当前会话；切换会话时收起并清空打开的标签
    closeFilePanel(true);
    // @ 引用弹层与目录缓存同样属于当前会话工作区
    hideFilePopup();
    clearFileRefCache();
    closeQuickMessagePanel();
    // 审批模式上拉框挂在 body 上，切换会话同样要收起，避免浮层残留
    closePermissionModeMenu();
  }
  const conversation = await api(`/api/conversations/${id}`);
  state.conversationId = id;
  if (conversation.workspace_dir) {
    state.workspaceDir = conversation.workspace_dir;
  }
  state.expandedGroups.add(currentConversationWorkspaceGroup());
  renderComposerWorkspace();
  const index = state.conversations.findIndex((item) => item.id === id);
  if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...conversation };
  state.conversationSnapshot = conversationSnapshot(conversation);
  console.log('[naiba] openConversation', id.slice(0, 8), '服务器返回消息数=', (conversation.messages || []).length);
  // 首轮上下文（系统提示词 + 工具集）折叠卡：单独拉取，失败静默（老会话无此数据）。
  try {
    const firstTurn = await api(`/api/conversations/${id}/first_turn`);
    const hasSystem = firstTurn && typeof firstTurn === 'object' && Boolean(firstTurn.system || firstTurn.prompt);
    state.firstTurnInfo = hasSystem ? firstTurn : null;
  } catch (_) {
    state.firstTurnInfo = null;
  }
  // 若打开的会话处于“最新 5 条”预览之外，自动展开该工作区的全部会话以便其在侧栏可见。
  const visGroup = currentConversationWorkspaceGroup();
  if (visGroup) {
    const wsConvs = state.conversations.filter((c) => (c.workspace_group || '').trim() === visGroup);
    const recent = [...wsConvs].sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    if (recent.findIndex((c) => c.id === id) >= SIDE_CONV_LIMIT) sidebarShowAll.add(visGroup);
  }
  sidebarScrollToActive = true;
  renderSidebar();
  applyConversationModel(conversation);
  applyConversationAgent(conversation);
  renderMessages(conversation.messages || []);
  renderPermissionModeSwitch();
  // 联网搜索开关由对话数据库字段恢复，不依赖当前浏览器。
  state.webSearchEnabled = Boolean(Number(conversation.web_search_enabled || 0));
  state.deepReasoningEnabled = Boolean(Number(conversation.deep_reasoning_enabled || 0));
  state.reasoningEffort = conversation.reasoning_effort || (state.deepReasoningEnabled ? 'medium' : 'auto');
  updateDeepReasoningButton();
  await resumeConversationRun(id);
  closeSidebar();
}

export function conversationSnapshot(conversation) {
  const messages = Array.isArray(conversation?.messages) ? conversation.messages : [];
  const last = messages.at(-1);
  return [conversation?.updated_at || '', messages.length, last?.id || '', last?.role || ''].join('|');
}

export async function syncCurrentConversation() {
  if (state.syncInFlight || !state.conversationId || state.abortController) return;
  if (document.visibilityState === 'hidden') return;
  state.syncInFlight = true;
  const id = state.conversationId;
  try {
    const conversation = await api(`/api/conversations/${id}`);
    if (state.conversationId !== id || state.abortController) return;
    const snapshot = conversationSnapshot(conversation);
    if (snapshot === state.conversationSnapshot) return;
    const index = state.conversations.findIndex((item) => item.id === id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...conversation };
    state.conversationSnapshot = snapshot;
    renderSidebar();
    renderMessages(conversation.messages || []);
    renderPermissionModeSwitch();
    state.webSearchEnabled = Boolean(Number(conversation.web_search_enabled || 0));
    state.deepReasoningEnabled = Boolean(Number(conversation.deep_reasoning_enabled || 0));
    updateDeepReasoningButton();
  } catch (error) {
    console.debug('[naiba] 对话同步失败:', error.message);
  } finally {
    state.syncInFlight = false;
  }
}

export function startConversationSync() {
  if (state.syncPolling) return;
  state.syncPolling = true;
  scheduleConversationSync(1800);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      if (state.syncTimer) window.clearTimeout(state.syncTimer);
      state.syncTimer = null;
    } else {
      scheduleConversationSync(0);
    }
  });
}

export function scheduleConversationSync(delay = null) {
  if (!state.syncPolling || document.visibilityState === 'hidden') return;
  if (state.syncTimer) window.clearTimeout(state.syncTimer);
  const activeTask = state.tasks.some((task) => activeTaskStatuses.has(task.status));
  const activeRun = Boolean(state.chatRunId || state.abortController || state.checkRunEligible);
  const interval = activeTask || activeRun ? 1800 : 10000;
  state.syncTimer = window.setTimeout(async () => {
    state.syncTimer = null;
    await syncCurrentConversation();
    scheduleConversationSync();
  }, delay ?? interval);
}

export function taskElapsed(task) {
  const start = Number(task.started_at || task.created_at || 0);
  const end = Number(task.finished_at || Date.now());
  if (!start || end < start) return '';
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

// 组标题与行内的时间：今天只给时分，昨天带前缀，更早带月日。
export function formatTaskTime(value) {
  const ms = Number(value || 0);
  if (!ms) return '时间未知';
  const at = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  const clock = `${pad(at.getHours())}:${pad(at.getMinutes())}`;
  const now = new Date();
  if (at.toDateString() === now.toDateString()) return clock;
  if (at.toDateString() === new Date(now.getTime() - 86400000).toDateString()) return `昨天 ${clock}`;
  return `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${clock}`;
}

// 组标题 = 类型摘要 + 时间（如「ComfyUI 生成 ×2 · 14:32」）。刻意不引用用户消息原文：
// 那句话可能含有不该在面板里复述的内容。
export function taskGroupTitle(tasks) {
  const counts = new Map();
  for (const task of tasks) {
    const name = taskKindLabel(task.kind);
    counts.set(name, (counts.get(name) || 0) + 1);
  }
  const summary = [...counts.entries()]
    .map(([name, count]) => (count > 1 ? `${name} ×${count}` : name))
    .join(' + ');
  const stamps = tasks.map((task) => Number(task.created_at) || 0).filter(Boolean);
  return `${summary} · ${formatTaskTime(stamps.length ? Math.min(...stamps) : 0)}`;
}

// 详情折叠里的键名：worker 写进 detail/result 的是英文键，常见几个给中文标签，
// 其余原样展示（只铺展标量，避免把整张快照塞进 DOM）。
const TASK_DETAIL_KEY_LABELS = { message: '说明', reason: '原因', command: '命令', exit_code: '退出码' };

function taskDetailRows(task) {
  const rows = [];
  const push = (label, value) => {
    const text = String(value ?? '').trim();
    if (!text) return;
    rows.push([label, text.length > 300 ? `${text.slice(0, 300)}…` : text]);
  };
  push('当前步骤', task.current_step);
  push('说明', task.detail?.message);
  push('错误', task.error);
  if (Number(task.attempt) > 0) push('轮询次数', `${Number(task.attempt)} 次`);
  const handle = task.checkpoint?.handle ?? task.result?.handle;
  if (handle !== undefined && handle !== null && handle !== '') push('外部句柄', handle);
  for (const [key, value] of Object.entries(task.detail || {})) {
    if (key === 'message' || value === null || typeof value === 'object') continue;
    push(TASK_DETAIL_KEY_LABELS[key] || key, value);
  }
  for (const [key, value] of Object.entries(task.result || {})) {
    if (value === null || typeof value === 'object') continue;
    push(`结果 · ${TASK_DETAIL_KEY_LABELS[key] || key}`, value);
  }
  if (task.checkpoint && Object.keys(task.checkpoint).length) {
    let text = '';
    try { text = JSON.stringify(task.checkpoint); } catch (error) { text = ''; }
    if (text) push('断点', text);
  }
  return rows.slice(0, 12);
}

function taskRowMarkup(task, nested = false) {
  const active = activeTaskStatuses.has(task.status);
  const note = String(task.error || task.detail?.message || task.current_step || '');
  const rows = taskDetailRows(task);
  const detailHtml = rows.length
    ? `<dl class="task-detail" hidden>${rows
      .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`)
      .join('')}</dl>`
    : '';
  // 「详情」入口常驻：它展开的不只是结构化字段，还有下面那段任务日志——把入口绑在
  // 「有没有详情行」上，会让「无标量字段、但有日志」的任务永远打不开日志（ComfyUI 的
  // result 常整块是对象，会被 taskDetailRows 跳过）。
  const detailButton = `<button type="button" class="task-more" data-task-detail="${escapeHtml(task.id)}" aria-expanded="false" aria-label="展开详情">详情</button>`;
  // 后台任务的逐行日志（展开详情时按 cursor 增量拉取）。
  // 日志行本来就以 job_log 事件写在 run_events 里，但前端一直没人消费它——「转后台以后
  // 还能实时看日志」在那之前是假前提，所以先把入口补上，再谈引导模型转后台。
  const logHtml = '<div class="task-log" hidden><div class="task-log-head">任务日志<span class="task-log-hint"></span></div>'
    + '<div class="task-log-lines" role="log" aria-label="后台任务实时输出"></div></div>';
  // 「跳转」是卡片上唯一的切会话入口：卡片本体不再整块可点——卡片上遍布可读内容
  // （任务名、说明、可选中文本、展开的日志），整块可点时一次误触就切走会话并关掉面板。
  // 位置固定在「详情」左侧（用户口径：跳转是显式动作，靠边不抢阅读区）。
  const openButton = `<button type="button" class="task-open" data-task-open="${escapeHtml(task.id)}" title="打开这个任务所属的对话">跳转</button>`;
  return `<div class="task-item${nested ? ' task-item-nested' : ''}" data-task-id="${escapeHtml(task.id)}">
      <div class="task-head">
        <span class="task-status ${escapeHtml(task.status)}">${escapeHtml(taskStatusLabel(task.status))}</span>
        <b class="task-kind" title="${escapeHtml(taskKindLabel(task.kind))}">${escapeHtml(taskDisplayTitle(task))}</b>
        <span class="task-elapsed">${escapeHtml(taskElapsed(task))}</span>
        ${note ? `<span class="task-note" title="${escapeHtml(note)}">${escapeHtml(note)}</span>` : ''}
      </div>
      <div class="task-actions">
        <span class="task-time" title="最后更新">${escapeHtml(formatTaskTime(task.updated_at || task.created_at))}</span>
        ${openButton}
        ${detailButton}
        ${active ? `<button type="button" class="task-cancel" data-task-cancel="${escapeHtml(task.id)}" aria-label="停止这个后台任务">停止</button>` : ''}
      </div>
      ${detailHtml}
      ${logHtml}
    </div>`;
}

// ---- 后台任务日志（job_log）：按 cursor 增量拉取，展开详情时可见 ----
// 数据来源：JobRegistry 逐行 emit 的 job_log 事件已经落在 run_events 里，
// `GET /api/jobs/<id>/events?after=<cursor>` 原样读回（后端无新增接口）。
// cursor 语义：只返回 sequence > cursor 的行，所以重复调用是幂等的，不会出现重复行。
// 但「cursor 只增」和「面板每轮轮询都整体重建 DOM」叠在一起会丢行：新节点里没有日志，
// 续拉只带回上一轮之后的增量，已看到的历史行会在每次重绘时凭空消失（展开着也只剩最近
// 一轮的新行，hint 还跳回「暂无输出」）。所以已消费的行必须自己留一份（taskLogLines），
// DOM 只当投影：重绘后先按缓存回填、再续拉增量；收起再展开同样靠缓存还原最近 200 行。
const taskLogCursors = new Map();   // taskId → 已消费到的 sequence
const taskLogLines = new Map();     // taskId → 已消费的行文本（重绘/重开日志时回填的依据）
const taskLogStick = new Map();     // taskId → 重绘前是否停在底部（回填时据此决定贴不贴底）
const taskLogLoading = new Set();   // taskId → 正在拉取（避免同一个 cursor 被并发消费两遍）
const openTaskLogs = new Set();     // 当前展开日志的 taskId（列表重渲染后据此恢复展开态）
const TASK_LOG_MAX_LINES = 200;     // 只保留最近 N 行：长任务的日志可以到几万行，不能无限铺 DOM

export function setTaskLogOpen(taskId, open) {
  const id = String(taskId || '');
  if (!id) return;
  if (open) {
    openTaskLogs.add(id);
    loadTaskLog(id);
  } else {
    openTaskLogs.delete(id);
  }
}

// 日志滚动位置（由 #taskList 的捕获阶段滚动委托实时上报）：用户上翻读历史时，
// 新行到来与整表重绘都不得把他拽回底部；回到贴底才继续跟随。
export function setTaskLogStick(taskId, stick) {
  const id = String(taskId || '');
  if (id) taskLogStick.set(id, Boolean(stick));
}

// 按 id 在面板内定位任务行：不拼属性选择器——任务 id 来自后端，含引号/反斜杠时会拼出
// 非法选择器直接抛错；也避免全局选择器命中面板外的同名 data 属性（如活动流气泡）。
function taskItemById(taskId) {
  const id = String(taskId || '');
  const list = $('#taskList');
  if (!id || !list) return null;
  for (const item of list.querySelectorAll('[data-task-id]')) {
    if (item.dataset.taskId === id) return item;
  }
  return null;
}

function taskLogBox(taskId) {
  const item = taskItemById(taskId);
  return item ? item.querySelector('.task-log') : null;
}

// 把行文本铺进日志框（回填缓存与追加增量共用）：统一裁剪到上限；贴底与否由调用方决定，
// 用户正在往上翻历史时不能被每轮重绘拽回底部。
function renderLogLines(lines, texts, stickToBottom = true) {
  if (!lines || !texts.length) return;
  const fragment = document.createDocumentFragment();
  for (const text of texts) {
    const line = document.createElement('div');
    line.className = 'task-log-line';
    line.textContent = text;
    fragment.appendChild(line);
  }
  lines.appendChild(fragment);
  while (lines.childElementCount > TASK_LOG_MAX_LINES) lines.removeChild(lines.firstElementChild);
  if (stickToBottom) lines.scrollTop = lines.scrollHeight;
}

function logHintText(lines) {
  return lines.childElementCount ? `最近 ${lines.childElementCount} 行` : '暂无输出';
}

async function loadTaskLog(taskId) {
  const log = taskLogBox(taskId);
  if (!log || log.hidden) return;
  const lines = log.querySelector('.task-log-lines');
  const hint = log.querySelector('.task-log-hint');
  if (!lines) return;
  // 重绘后的空节点先按缓存回填：这一步不看 cursor，历史行不会因为重绘而丢。
  if (!lines.childElementCount) renderLogLines(lines, taskLogLines.get(taskId) || [], taskLogStick.get(taskId) !== false);
  if (hint) hint.textContent = logHintText(lines);
  if (taskLogLoading.has(taskId)) return;
  taskLogLoading.add(taskId);
  const after = taskLogCursors.get(taskId) || 0;
  let payload = null;
  try {
    payload = await api(`/api/jobs/${encodeURIComponent(taskId)}/events?after=${after}`);
  } catch (error) {
    if (hint) hint.textContent = lines.childElementCount ? `最近 ${lines.childElementCount} 行 · 读取失败` : '读取失败';
    return;
  } finally {
    taskLogLoading.delete(taskId);
  }
  const events = Array.isArray(payload?.events) ? payload.events : [];
  const cursor = Number(payload?.cursor || 0);
  if (cursor > after) taskLogCursors.set(taskId, cursor);
  // 先写缓存再落 DOM：拉取期间列表可能已经重绘，此时新节点里没有这些行，下一轮
  // restore 会按缓存回填。缓存是唯一真相，DOM 丢了不影响内容完整性。
  const fresh = [];
  for (const event of events) {
    const text = String(event?.line ?? '').replace(/\s+$/, '');
    if (text) fresh.push(text);
  }
  if (fresh.length) {
    const cache = taskLogLines.get(taskId) || [];
    cache.push(...fresh);
    while (cache.length > TASK_LOG_MAX_LINES) cache.shift();
    taskLogLines.set(taskId, cache);
  }
  // await 之后重新取一次节点：旧节点可能已脱离文档，不能把新行写进「尸体」。
  const liveLog = taskLogBox(taskId);
  const liveLines = liveLog ? liveLog.querySelector('.task-log-lines') : null;
  if (!liveLines) return;
  if (!liveLines.childElementCount) renderLogLines(liveLines, taskLogLines.get(taskId) || [], taskLogStick.get(taskId) !== false);
  else renderLogLines(liveLines, fresh, taskLogStick.get(taskId) !== false);
  const liveHint = liveLog.querySelector('.task-log-hint');
  if (liveHint) liveHint.textContent = logHintText(liveLines);
}

// 列表整体重渲染后恢复展开态（并把详情面板与日志一起展开），再续拉一次增量日志。
function restoreOpenTaskLogs() {
  for (const id of [...openTaskLogs]) {
    const item = taskItemById(id);
    const log = item ? item.querySelector('.task-log') : null;
    if (!log) {
      // 任务已从列表消失（清理/删除）：展开态与日志缓存一并回收，别让 Map 只涨不落。
      openTaskLogs.delete(id);
      taskLogCursors.delete(id);
      taskLogLines.delete(id);
      taskLogStick.delete(id);
      continue;
    }
    log.hidden = false;
    const detail = item.querySelector('.task-detail');
    if (detail) detail.hidden = false;
    const button = item.querySelector('[data-task-detail]');
    if (button) {
      button.setAttribute('aria-expanded', 'true');
      button.textContent = '收起';
    }
    loadTaskLog(id);
  }
}

function renderTaskSummary(jobs, active) {
  const box = $('#taskSummary');
  if (!box) return;
  const failed = jobs.filter((task) => task.status === 'failed').length;
  const completed = jobs.filter((task) => task.status === 'completed').length;
  const stats = `共 ${jobs.length} · 运行中 ${active.length} · 失败 ${failed} · 已完成 ${completed}`;
  const stale = String(state.taskSyncFailed || '');
  if (!stale) {
    box.classList.remove('is-stale');
    box.textContent = stats;
    return;
  }
  // 轮询失败：如实说出原因与「最后成功更新」，而不是默默展示上一次的旧状态。
  const last = state.taskSyncedAt ? `最后成功更新：${formatTaskTime(state.taskSyncedAt)}` : '尚未成功同步过';
  box.classList.add('is-stale');
  box.textContent = `${stats} · 同步失败：${stale}（${last}）`;
}

export function renderRunTasks() {
  // 面板只列后台作业：chat/plan_execute 那些行是「每一次回答的记录」，不是任务
  // （后端 jobs_only=1 已过滤，这里再兜一层防止旧数据/旧缓存混进来）。
  const jobs = (state.tasks || []).filter((task) => !['chat', 'plan_execute'].includes(String(task.kind || '')));
  const active = jobs.filter((task) => activeTaskStatuses.has(task.status));
  // 徽标：有活动任务时显示活动数；全部结束但有历史时显示总数并弱化——
  // 否则跑完就变 0，用户会以为从来没有过任务（截图里的「0 任务」就是这个原因）。
  const badge = $('#taskCount');
  if (badge) {
    badge.textContent = String(active.length || jobs.length);
    badge.classList.toggle('is-idle', active.length === 0 && jobs.length > 0);
  }
  $('#openTasks')?.classList.toggle('has-active', active.length > 0);
  const current = active.filter((task) => task.conversation_id === state.conversationId);
  const bar = $('#activeTaskBar');
  if (bar) {
    bar.hidden = current.length === 0;
    if (current.length) {
      bar.innerHTML = `当前对话有 ${current.length} 个后台任务正在执行。<button type="button" data-open-tasks>查看</button>`;
    }
  }
  renderTaskSummary(jobs, active);
  const list = $('#taskList');
  if (!list) return;
  if (!jobs.length) {
    list.innerHTML = '<div class="task-empty">暂无异步任务</div>';
    // 任务全被清理时顺手回收展开态与日志缓存（restore 找不到对应行会自己删）。
    restoreOpenTaskLogs();
    return;
  }
  // 同一批作业（同一个父回答派生）归一组；父行不在返回里（已被过滤）时按"无父作业"单独成组。
  // 分组 key 必须沿**可见父链**上溯到锚点：链路是 chat → 子Agent → ComfyUI 两层，
  // chat 行被 jobs_only 过滤后，若只按单层 parent_job_id 分组，子 Agent 与它派生的
  // ComfyUI 任务会裂成两组（用户看到"一个子 Agent 又分成两个任务"）。
  // 锚点口径：① 无父作业仍用 ''（沿用旧口径，同批无父作业归一组）；
  // ② 父不在列表（chat 行被过滤/记录已清理）→ 用那个**父 id** 当锚点，兄弟作业不拆开；
  // ③ 父在列表就继续上溯，环用 seen 兜住（脏数据不得把面板卡死）。
  const byId = new Map(jobs.map((task) => [String(task.id), task]));
  const rootKeyOf = (task) => {
    let key = String(task.parent_job_id || '');
    if (!key) return '';
    const seen = new Set([String(task.id), key]);
    let parent = byId.get(key);
    while (parent) {
      const next = String(parent.parent_job_id || '');
      if (!next || seen.has(next)) break; // 可见顶层 / 成环：停在这里，父行与子行同组
      seen.add(next);
      key = next;
      parent = byId.get(next);
    }
    return key;
  };
  const groups = new Map();
  for (const task of jobs) {
    const key = rootKeyOf(task);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(task);
  }
  const ordered = [...groups.values()]
    .map((tasks) => {
      const sorted = [...tasks].sort((a, b) => Number(a.created_at || 0) - Number(b.created_at || 0));
      return { tasks: sorted, latest: Math.max(...sorted.map((task) => Number(task.created_at) || 0)) };
    })
    .sort((a, b) => b.latest - a.latest);
  // 重绘前先记下「用户是否停在日志底部」：restore 回填时据此决定贴不贴底，
  // 别让每轮轮询的重绘把正在往上翻历史的人拽回底部（采集必须在 innerHTML 覆盖之前，
  // 覆盖之后读到的是新的空节点，永远算出"贴底"）。
  for (const id of [...openTaskLogs]) {
    const lines = taskItemById(id)?.querySelector('.task-log-lines');
    if (lines) taskLogStick.set(id, lines.scrollHeight - lines.scrollTop - lines.clientHeight < 12);
  }
  list.innerHTML = ordered.map(({ tasks }) => {
    const failed = tasks.filter((task) => task.status === 'failed').length;
    return `<section class="task-group">
      <div class="task-group-head">
        <span class="task-group-title">${escapeHtml(taskGroupTitle(tasks))}</span>
        <span class="task-group-stat">共 ${tasks.length}${failed ? ` · 失败 ${failed}` : ''}</span>
      </div>
      ${tasks.map((task) => taskRowMarkup(task, byId.has(String(task.parent_job_id || '')))).join('')}
    </section>`;
  }).join('');
  // 列表是整体重渲染的，展开态与日志内容都要就地恢复（日志按缓存回填 + cursor 续拉，不会重复）。
  restoreOpenTaskLogs();
}

// ---- Agent 设置页：快捷提示词（套用 / 另存 / 删除）+ 角色卡导入 ----
// 会话级系统提示词已移除：系统提示词只有一个来源（Agent），因此快捷提示词的入口
// 全部落在 Agent 编辑表单的「系统提示词（预设与规则）」下方（设置页的快捷提示词页已下线）。
const AGENT_PROMPT_LIMIT = 12000; // 与 naiba/config.py 里 Agent system_prompt 的截断上限一致
const PRESET_EDIT_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19.5 8.5a2.12 2.12 0 0 0-3-3L5 17l-1 4Z"></path><path d="M13.5 6.5l3 3"></path></svg>';
const PRESET_DELETE_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>';

// 纯函数（可单测）：把角色卡文本追加到 Agent 系统提示词末尾（不覆盖已有内容）。
export function mergeAgentPromptText(existing, addition, limit = AGENT_PROMPT_LIMIT) {
  const base = String(existing || '').trim();
  const extra = String(addition || '').trim();
  if (!extra) return { text: base, truncated: false };
  const merged = base ? `${base}\n\n${extra}` : extra;
  if (merged.length > limit) return { text: merged.slice(0, limit), truncated: true };
  return { text: merged, truncated: false };
}

export async function loadConversationPromptPresets() {
  try {
    const result = await api('/api/conversation-prompt-presets');
    state.conversationPromptPresets = Array.isArray(result.presets) ? result.presets : [];
  } catch (error) {
    state.conversationPromptPresets = [];
  }
  renderAgentPromptPresetList();
}

// 面板列表：标题 + 一行预览，右侧 × 直接删除（点条目本体 = 套用）。
export function renderAgentPromptPresetList() {
  const list = $('#agentPromptPresetList');
  if (!list) return;
  if (!state.conversationPromptPresets.length) {
    list.innerHTML = '<div class="quick-msg-empty">还没有快捷提示词：在系统提示词下方点「存为快捷提示词」保存一条</div>';
    return;
  }
  list.innerHTML = state.conversationPromptPresets.map((item) => {
    const text = String(item.system_prompt || '').replace(/\s+/g, ' ').trim();
    const preview = text.slice(0, 90);
    return `<div class="quick-msg-item" role="menuitem" tabindex="-1" data-agent-preset="${escapeHtml(item.id)}" title="点击套用到上方系统提示词">
      <div class="quick-msg-main">
        <b>${escapeHtml(item.title)}</b>
        ${preview ? `<small>${escapeHtml(preview)}${text.length > 90 ? '…' : ''}</small>` : ''}
      </div>
      <button type="button" class="quick-msg-action" data-agent-preset-edit="${escapeHtml(item.id)}" title="编辑标题与正文" aria-label="编辑">${PRESET_EDIT_SVG}</button>
      <button type="button" class="quick-msg-action" data-agent-preset-delete="${escapeHtml(item.id)}" title="删除这条快捷提示词" aria-label="删除">${PRESET_DELETE_SVG}</button>
    </div>`;
  }).join('');
}

export function closeAgentPromptPresetPanel() {
  const panel = $('#agentPromptPresetPanel');
  const button = $('#agentPromptPresetButton');
  if (panel) panel.hidden = true;
  button?.setAttribute('aria-expanded', 'false');
}

// 面板固定定位：与按钮左对齐、优先向下展开，贴边时上翻并夹在视口内。
// 面板挂在模态 <dialog> 内部（top layer，body 上的 fixed 会被盖住），fixed 不受祖先 overflow 裁剪。
export function positionAgentPromptPresetPanel() {
  const panel = $('#agentPromptPresetPanel');
  const button = $('#agentPromptPresetButton');
  if (!panel || !button || panel.hidden) return;
  const rect = button.getBoundingClientRect();
  const width = panel.offsetWidth;
  const height = panel.offsetHeight;
  const margin = 8;
  let top = rect.bottom + 6;
  if (top + height > window.innerHeight - margin) top = Math.max(margin, rect.top - height - 6);
  const left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin));
  panel.style.top = `${Math.max(margin, top)}px`;
  panel.style.left = `${left}px`;
}

export async function toggleAgentPromptPresetPanel() {
  const panel = $('#agentPromptPresetPanel');
  const button = $('#agentPromptPresetButton');
  if (!panel) return;
  if (!panel.hidden) {
    closeAgentPromptPresetPanel();
    return;
  }
  panel.hidden = false;
  button?.setAttribute('aria-expanded', 'true');
  // 每次打开都重新取：可能刚在「存为快捷提示词」里存过新的。
  await loadConversationPromptPresets();
  if (panel.hidden) return;
  positionAgentPromptPresetPanel();
}

export function applyAgentPromptPreset(id) {
  const item = state.conversationPromptPresets.find((preset) => preset.id === id);
  const field = $('#agentSystemPromptEdit');
  if (!item || !field) return;
  const next = String(item.system_prompt || '');
  const current = field.value.trim();
  if (current && current !== next.trim() && !confirm('当前系统提示词已有内容，是否用这条快捷提示词覆盖？')) return;
  field.value = next;
  closeAgentPromptPresetPanel();
  toast(`已套用快捷提示词「${item.title}」，保存 Agent 后生效`);
}

export async function removeAgentPromptPreset(id) {
  const item = state.conversationPromptPresets.find((preset) => preset.id === id);
  if (!item) return;
  if (!confirm(`确定删除快捷提示词「${item.title}」吗？`)) return;
  try {
    await api(`/api/conversation-prompt-presets/${encodeURIComponent(id)}`, { method: 'DELETE' });
    await loadConversationPromptPresets();
    positionAgentPromptPresetPanel();
    toast('已删除快捷提示词');
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

// 面板内事件委托：✎ 编辑 / × 删除优先于条目套用。
export function handleAgentPromptPresetPanelClick(event) {
  const edit = event.target.closest('[data-agent-preset-edit]');
  if (edit) {
    event.stopPropagation();
    openAgentPromptPresetEditDialog(edit.dataset.agentPresetEdit);
    return;
  }
  const remove = event.target.closest('[data-agent-preset-delete]');
  if (remove) {
    event.stopPropagation();
    void removeAgentPromptPreset(remove.dataset.agentPresetDelete);
    return;
  }
  const entry = event.target.closest('[data-agent-preset]');
  if (entry) applyAgentPromptPreset(entry.dataset.agentPreset);
}

export async function importAgentCharacterCard(file) {
  if (!file) return;
  try {
    const result = await api('/api/character-card/parse', {
      method: 'POST',
      body: { name: file.name, data: await readAsDataUrl(file) },
    });
    const prompt = String(result.system_prompt || '').trim();
    if (!prompt) {
      toast('角色卡解析结果为空');
      return;
    }
    const field = $('#agentSystemPromptEdit');
    if (!field) return;
    const merged = mergeAgentPromptText(field.value, prompt);
    field.value = merged.text;
    const cardName = result.meta?.name || file.name;
    if (merged.truncated) {
      toast(`已追加角色卡「${cardName}」，但超出 ${AGENT_PROMPT_LIMIT} 字符上限，已截断`);
    } else {
      toast(`已追加角色卡「${cardName}」，保存 Agent 后生效`);
    }
  } catch (error) {
    toast(`导入失败：${error.message}`);
  }
}

// 存为快捷提示词：正文预填当前系统提示词（可改），标题默认取正文首行。
export function openAgentPromptPresetSaveDialog() {
  const field = $('#agentSystemPromptEdit');
  const text = String(field?.value || '').trim();
  if (!text) {
    toast('系统提示词为空，先写点内容再保存');
    field?.focus();
    return;
  }
  state.agentPromptPresetEditingId = '';
  const firstLine = text.split('\n').map((line) => line.trim()).find(Boolean) || '';
  $('#promptPresetDialogTitle').textContent = '存为快捷提示词';
  $('#promptPresetTitle').value = firstLine.slice(0, 40);
  $('#promptPresetText').value = text;
  $('#promptPresetHint').textContent = `正文取自当前系统提示词（${text.length} 字符），可再修改`;
  $('#promptPresetDialog').showModal();
  $('#promptPresetTitle').focus();
  $('#promptPresetTitle').select();
}

// 编辑已有快捷提示词：标题 + 正文都可改，保存走同一接口（带 id 即覆盖）。
export function openAgentPromptPresetEditDialog(id) {
  const item = state.conversationPromptPresets.find((preset) => preset.id === id);
  if (!item) return;
  state.agentPromptPresetEditingId = id;
  $('#promptPresetDialogTitle').textContent = '编辑快捷提示词';
  $('#promptPresetTitle').value = String(item.title || '');
  $('#promptPresetText').value = String(item.system_prompt || '');
  $('#promptPresetHint').textContent = '保存后覆盖这条快捷提示词（已套用到 Agent 的文本不会被改动）';
  $('#promptPresetDialog').showModal();
  $('#promptPresetTitle').focus();
  $('#promptPresetTitle').select();
}

export async function saveAgentPromptPreset(event) {
  event.preventDefault();
  const id = String(state.agentPromptPresetEditingId || '');
  const title = String($('#promptPresetTitle').value || '').trim();
  const text = String($('#promptPresetText').value || '').trim();
  if (!text) {
    toast('正文不能为空');
    $('#promptPresetText').focus();
    return;
  }
  if (!title) {
    toast('请填写标题');
    $('#promptPresetTitle').focus();
    return;
  }
  const button = $('#savePromptPreset');
  if (button) button.disabled = true;
  try {
    const path = id
      ? `/api/conversation-prompt-presets/${encodeURIComponent(id)}`
      : '/api/conversation-prompt-presets';
    await api(path, { method: 'POST', body: { title, system_prompt: text, source: 'manual' } });
    state.agentPromptPresetEditingId = '';
    $('#promptPresetDialog').close();
    await loadConversationPromptPresets();
    positionAgentPromptPresetPanel();
    toast(id ? `已更新快捷提示词「${title}」` : `已存为快捷提示词「${title}」`);
  } catch (error) {
    toast(`保存失败：${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

// 停止单个异步任务：Job（comfyui / shell / check / http_poll / subagent）走 Job Registry 的
// 取消接口（会真正 set 掉 worker 的 cancel 事件）；chat / plan_execute 这类顶层 Run 走原有接口。
// 顶层 Run 才会占用对话互斥位，所以这两条路径必须分开，不能统一按任务 ID 处理。
export async function cancelTask(taskId) {
  const task = state.tasks.find((item) => item.id === taskId);
  const kind = String(task?.kind || 'chat');
  const isJob = kind !== 'chat' && kind !== 'plan_execute';
  try {
    if (isJob) {
      await api(`/api/jobs/${encodeURIComponent(taskId)}/cancel`, {
        method: 'POST',
        body: { conversation_id: task?.conversation_id || '' },
      });
    } else {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'DELETE' });
    }
    toast('已请求停止任务');
  } catch (error) {
    toast(`停止失败：${error.message}`);
  }
  await loadTasks();
}

export async function clearTerminalTasks() {
  if (!confirm('清理所有已结束、失败、取消或中断的异步任务记录吗？运行中的任务不会受影响。')) return;
  try {
    const result = await api('/api/tasks/clear', { method: 'DELETE' });
    await loadTasks();
    toast(`已清理 ${Number(result.deleted || 0)} 个任务`);
  } catch (error) {
    toast(`清理失败：${error.message}`);
  }
}

export async function deleteConversation(id) {
  closeConversationMenu();
  if (id === state.conversationId && state.chatRunId) {
    toast('请先停止当前回复再删除对话');
    return;
  }
  const conversation = state.conversations.find((item) => item.id === id);
  if (!confirm(`删除对话"${conversation?.title || '新对话'}"？`)) return;
  await api(`/api/conversations/${id}`, { method: 'DELETE' });
  const wasCurrent = state.conversationId === id;
  state.conversations = state.conversations.filter((item) => item.id !== id);
  if (wasCurrent) state.conversationId = '';
  renderSidebar();
  // 删除非当前会话：只刷新侧栏。绝不能重新加载当前会话视图，否则会把正在流式
  // 输出的消息行挤出 DOM（renderMessages 重建列表），导致流式输出消失、要等 run
  // 结束重新渲染才重现。当前会话的流式输出必须保持原样继续。
  if (!wasCurrent) return;
  if (state.conversations.length) await openConversation(state.conversations[0].id);
  else {
    // 最后一个会话被删除：清掉其 firstTurnInfo 残留，避免空视图错误展示首轮上下文条
    state.firstTurnInfo = null;
    renderMessages([]);
  }
}

// 当前对话绑定的 Agent 的固定 Skill id 列表；未绑定或已删除时回退到默认 Agent
export function currentAgentFixedSkillIds() {
  const agents = state.bootstrap?.agents || [];
  const conversation = state.conversations.find((item) => item.id === state.conversationId);
  let agentId = String(conversation?.agent_id || '');
  let agent = agents.find((item) => item.id === agentId);
  if (!agent) {
    agentId = String(state.bootstrap?.default_agent_id || '');
    agent = agents.find((item) => item.id === agentId);
  }
  return (agent?.skill_ids || []).map(String);
}

// 有效启用的 Skill = 仅当前会话 Agent 预设的固定 Skill（不再允许用户自行选择/切换模式）。
export function effectiveSkillIds() {
  return [...new Set(currentAgentFixedSkillIds())];
}

