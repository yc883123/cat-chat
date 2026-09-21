// ============================================================
// 16-file-refs.js —— 输入框 @ 引用会话工作区文件/目录（弹层 + 懒加载目录浏览）
//
// 触发：行首或空格后输入 @（与 / 引用 Skill 同源思路，两者互斥）。
// 交互：↑↓ 选择；Tab 进入高亮目录 / Shift+Tab 返回上级；目录项 Enter（或点击行）= 引用该
//       目录、右侧箭头按钮 = 进入目录（手机端无 Tab 键的替代入口）；文件项 Tab/Enter/点击 =
//       插入引用；列表首项「返回上一级」用于触屏逐级退出。
// 发送时由后端把 @相对路径 解析为工作区内的绝对路径（见 core/conv_files.resolve_file_references）。
// ============================================================

import { $, api, escapeHtml, notifyComposerChanged, state } from "./01-core.js";
import { addFolderChip } from "./10-upload.js";
import { positionComposerPopup, renderInputMirror, resizeTextarea } from "./13-skill-refs.js";

const DIR_CACHE = new Map();   // `${conversationId}|${rel}` → browse 响应（会话内复用）
const INFLIGHT = new Map();    // 同目录并发请求合并
const MAX_ROWS = 200;          // 弹层最多渲染条目（服务端已限 500，这里再兜渲染开销）

export const filePopupState = {
  open: false,
  selectedIndex: 0,
  items: [],
  token: null,
  listing: null,
  loading: false,
  error: '',
};

function browseKey(rel) {
  return `${state.conversationId}|${rel || ''}`;
}

function fetchDir(rel) {
  const key = browseKey(rel);
  if (DIR_CACHE.has(key)) return Promise.resolve(DIR_CACHE.get(key));
  if (INFLIGHT.has(key)) return INFLIGHT.get(key);
  const params = new URLSearchParams({ path: rel || '' });
  if (state.conversationId) params.set('conversation_id', state.conversationId);
  const promise = api(`/api/workspace/browse?${params.toString()}`).then(
    (listing) => { DIR_CACHE.set(key, listing); INFLIGHT.delete(key); return listing; },
    (error) => { INFLIGHT.delete(key); throw error; },
  );
  INFLIGHT.set(key, promise);
  return promise;
}

// 切换会话/工作区后目录缓存必须失效（相对路径相同但根不同）。
export function clearFileRefCache() {
  DIR_CACHE.clear();
  INFLIGHT.clear();
}

// 光标所在的 @ 引用 token；无效（不在 @ 后 / 已越过空白）返回 null。
export function currentAtToken(value, cursor) {
  if (!value || cursor == null) return null;
  const before = value.slice(0, cursor);
  const at = before.lastIndexOf('@');
  if (at < 0) return null;
  if (at > 0 && !/\s/.test(value[at - 1])) return null;
  if (value[at + 1] === '"') {
    const close = value.indexOf('"', at + 2);
    if (close >= 0 && close < cursor) return null;  // 引号已闭合：不再是活动 token
    return {
      tokenStart: at,
      tokenEnd: close >= 0 ? close + 1 : value.length,
      typed: value.slice(at + 2, cursor),
      quoted: true,
    };
  }
  const typed = value.slice(at + 1, cursor);
  if (/\s/.test(typed)) return null;
  let end = cursor;
  while (end < value.length && !/\s/.test(value[end])) end += 1;
  return { tokenStart: at, tokenEnd: end, typed, quoted: false };
}

// token 文本拆成「当前目录相对路径 + 名称过滤词」。
function splitTyped(typed) {
  const norm = String(typed || '').replace(/\\/g, '/');
  const slash = norm.lastIndexOf('/');
  if (slash < 0) return { dir: '', query: norm };
  return { dir: norm.slice(0, slash), query: norm.slice(slash + 1) };
}

// 输入框里的 token 文本（导航用：目录 + 尾斜杠；含空格/引号时加引号）。
function navTokenText(rel) {
  const body = rel ? `${String(rel).replace(/\\/g, '/')}/` : '';
  return /[\s"]/.test(body) ? `@"${body}"` : `@${body}`;
}

// 插入到输入框的引用文本（目录保留尾斜杠，便于后端与模型识别为目录）。
function insertTokenText(item) {
  const rel = String(item.rel || '').replace(/\\/g, '/');
  const body = item.kind === 'directory' ? `${rel}/` : rel;
  return /[\s"]/.test(body) ? `@"${body}"` : `@${body}`;
}

function replaceActiveToken(text, { keepPopup = false } = {}) {
  const input = $('#messageInput');
  if (!input) return;
  const token = currentAtToken(input.value, input.selectionStart) || filePopupState.token;
  const value = input.value;
  const start = token ? token.tokenStart : input.selectionStart;
  const end = token ? token.tokenEnd : input.selectionStart;
  input.value = value.slice(0, start) + text + value.slice(end);
  const cursor = start + text.length;
  input.setSelectionRange(cursor, cursor);
  resizeTextarea();
  renderInputMirror();
  // @ 引用同样是程序化改值：通知输入管线，发送按钮才会随内容刷新。
  notifyComposerChanged(input);
  if (keepPopup) updateFilePopup();
}

function formatSize(size) {
  if (!Number.isFinite(size)) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function iconSvg(kind) {
  if (kind === 'directory') {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"></path></svg>';
  }
  if (kind === 'parent') {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"></path></svg>';
  }
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"></path><path d="M14 3v5h5"></path></svg>';
}

function buildItems(listing, query) {
  const entries = Array.isArray(listing?.entries) ? listing.entries : [];
  const q = String(query || '').toLowerCase();
  const matched = q
    ? entries.filter((entry) => String(entry?.name || '').toLowerCase().includes(q))
    : entries;
  const items = matched.slice(0, MAX_ROWS);
  if (String(listing?.rel || '')) {
    items.unshift({ kind: 'parent', name: '..', rel: listing.parent_rel || '', path: listing.parent || '' });
  }
  return items;
}

function rowHtml(item, index) {
  const kind = String(item?.kind || 'file');
  const isParent = kind === 'parent';
  const isDir = kind === 'directory';
  const selected = index === filePopupState.selectedIndex;
  const label = isParent ? '返回上一级' : String(item?.name || '');
  const meta = isParent ? '' : (isDir ? '目录' : formatSize(item?.size));
  const hint = isDir ? '<span class="file-popup-tab" aria-hidden="true">Tab</span>' : '';
  const tip = isParent
    ? '返回上级目录'
    : (isDir ? '点击引用该目录（Tab 或 › 进入）' : '点击引用该文件');
  const enter = (isDir || isParent)
    ? `<button type="button" class="file-popup-enter" data-file-enter="${index}" title="${isParent ? '返回上级目录' : '进入目录'}" aria-label="${isParent ? '返回上级目录' : '进入目录'}">${isParent ? '↑' : '›'}</button>`
    : '';
  // 目录行多一个「＋」：把整个文件夹加进待发送（只带路径清单，不铺缩略图）。
  // 这是拖拽之外的**第二条入口**，也是手机上唯一可用的那条——触屏没有 drop 事件。
  // 刻意不用"目录行可拖拽"：本弹层的 mousedown 被 preventDefault 以保住输入框焦点，
  // 而 Chromium 的拖拽正是由 mousedown 默认行为发起的（§九.120）。
  const add = isDir
    ? `<button type="button" class="file-popup-add" data-file-folder="${index}" title="把整个文件夹加入待发送（只索引路径与文件名，不上传内容）" aria-label="加入待发送">＋</button>`
    : '';
  return `<div class="file-popup-item${selected ? ' selected' : ''}" role="option" aria-selected="${selected ? 'true' : 'false'}" data-file-index="${index}" data-kind="${kind}" title="${escapeHtml(tip)}">
    <span class="file-popup-icon">${iconSvg(kind)}</span>${hint}
    <span class="file-popup-name">${escapeHtml(label)}</span>
    <small class="file-popup-meta">${escapeHtml(meta)}</small>${add}${enter}
  </div>`;
}

export function renderFilePopup() {
  const popup = $('#filePopup');
  if (!popup) return;
  filePopupState.open = true;
  const listing = filePopupState.listing;
  const items = filePopupState.items;
  const crumb = listing
    ? (String(listing.rel || '') ? `工作区 / ${listing.rel}` : '工作区根目录')
    : '工作区';
  let body = '';
  if (filePopupState.error) {
    body = `<div class="file-popup-empty">${escapeHtml(filePopupState.error)}</div>`;
  } else if (filePopupState.loading) {
    body = '<div class="file-popup-empty">正在读取目录…</div>';
  } else if (!items.length) {
    body = '<div class="file-popup-empty">没有匹配的文件或文件夹</div>';
  } else {
    body = items.map((item, index) => rowHtml(item, index)).join('');
  }
  const note = listing?.truncated ? '<span class="file-popup-note">仅显示前 500 项</span>' : '';
  popup.innerHTML = `<div class="file-popup-head"><span class="file-popup-crumb">${escapeHtml(crumb)}</span>${note}</div>`
    + body
    + '<div class="file-popup-foot">↑↓ 选择 · Tab 进入目录 · Shift+Tab 返回 · Enter 引用</div>';
  popup.hidden = false;
  positionComposerPopup(popup);
  popup.querySelector('.selected')?.scrollIntoView({ block: 'nearest' });
}

export function hideFilePopup() {
  filePopupState.open = false;
  filePopupState.items = [];
  filePopupState.token = null;
  filePopupState.listing = null;
  filePopupState.error = '';
  filePopupState.loading = false;
  filePopupState.selectedIndex = 0;
  const popup = $('#filePopup');
  if (popup) popup.hidden = true;
}

export function positionFilePopup() {
  positionComposerPopup($('#filePopup'));
}

export function setFilePopupSelection(index) {
  const items = filePopupState.items;
  if (!items.length) return;
  const next = ((index % items.length) + items.length) % items.length;
  filePopupState.selectedIndex = next;
  const popup = $('#filePopup');
  popup?.querySelectorAll('[data-file-index]').forEach((el) => {
    const active = Number(el.dataset.fileIndex) === next;
    el.classList.toggle('selected', active);
    el.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  popup?.querySelector('.selected')?.scrollIntoView({ block: 'nearest' });
}

function applyListing(listing, query, token) {
  if (filePopupState.token !== token) return;  // 输入已变化：丢弃过期响应
  filePopupState.listing = listing;
  filePopupState.error = '';
  filePopupState.loading = false;
  filePopupState.items = buildItems(listing, query);
  filePopupState.selectedIndex = 0;
  renderFilePopup();
}

async function loadDir(dirRel, query, token) {
  const key = browseKey(dirRel);
  if (DIR_CACHE.has(key)) {
    applyListing(DIR_CACHE.get(key), query, token);
    return;
  }
  filePopupState.loading = true;
  filePopupState.error = '';
  filePopupState.listing = null;
  filePopupState.items = [];
  renderFilePopup();
  try {
    const listing = await fetchDir(dirRel);
    applyListing(listing, query, token);
  } catch (error) {
    if (filePopupState.token !== token) return;
    filePopupState.loading = false;
    filePopupState.listing = null;
    filePopupState.items = [];
    filePopupState.error = error?.message || '目录读取失败';
    renderFilePopup();
  }
}

export function updateFilePopup() {
  const input = $('#messageInput');
  if (!input) return hideFilePopup();
  const token = currentAtToken(input.value, input.selectionStart);
  if (!token) return hideFilePopup();
  const { dir, query } = splitTyped(token.typed);
  filePopupState.token = token;
  loadDir(dir, query, token);
}

function enterDir(item) {
  const input = $('#messageInput');
  if (!input) return;
  replaceActiveToken(navTokenText(String(item.rel || '')), { keepPopup: true });
  input.focus();
}

function goToParent() {
  const input = $('#messageInput');
  if (!input) return;
  const parentRel = filePopupState.listing ? String(filePopupState.listing.parent_rel || '') : '';
  replaceActiveToken(navTokenText(parentRel), { keepPopup: true });
  input.focus();
}

function commitFileSelection(item) {
  if (!item) return;
  if (item.kind === 'parent') { goToParent(); return; }
  const input = $('#messageInput');
  if (!input) return;
  replaceActiveToken(`${insertTokenText(item)} `);
  hideFilePopup();
  input.focus();
}

// 键盘：返回 true 表示已消费该按键（调用方不再执行默认行为）。
export function handleFilePopupKey(event) {
  if (!filePopupState.open) return false;
  // 输入法组词期间不抢键（Enter/Tab/方向键交给输入法）。
  if (event.isComposing) return false;
  const items = filePopupState.items;
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    if (!items.length) return true;
    event.preventDefault();
    setFilePopupSelection(filePopupState.selectedIndex + (event.key === 'ArrowDown' ? 1 : -1));
    return true;
  }
  if (event.key === 'Escape') {
    event.preventDefault();
    hideFilePopup();
    return true;
  }
  if (event.key === 'Tab') {
    const item = items[filePopupState.selectedIndex];
    if (!item) return false;
    event.preventDefault();
    if (event.shiftKey) { goToParent(); return true; }
    if (item.kind === 'directory') { enterDir(item); return true; }
    commitFileSelection(item);
    return true;
  }
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    const item = items[filePopupState.selectedIndex];
    if (!item) return false;
    event.preventDefault();
    commitFileSelection(item);
    return true;
  }
  return false;
}

// 点击委托：右侧箭头/「返回上一级」箭头 = 进入，其余区域 = 引用（目录也可被引用）。
export function handleFilePopupClick(event) {
  const folderButton = event.target.closest?.('[data-file-folder]');
  if (folderButton) {
    event.preventDefault();
    const target = filePopupState.items[Number(folderButton.dataset.fileFolder)];
    if (target?.path) void addFolderChip(target.path);
    return;
  }
  const enterButton = event.target.closest?.('[data-file-enter]');
  if (enterButton) {
    event.preventDefault();
    const item = filePopupState.items[Number(enterButton.dataset.fileEnter)];
    if (!item) return;
    if (item.kind === 'parent') goToParent();
    else enterDir(item);
    return;
  }
  const row = event.target.closest?.('[data-file-index]');
  if (!row) return;
  event.preventDefault();
  commitFileSelection(filePopupState.items[Number(row.dataset.fileIndex)]);
}
