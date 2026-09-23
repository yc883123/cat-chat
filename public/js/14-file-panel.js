// ============================================================
// 14-file-panel.js —— 拆分自 public/app.js 第 6329-6657 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, escapeHtml, isCoarsePointer, state, toast } from "./01-core.js";
import { renderSidebar } from "./08-conversations.js";
import { markdownFilePreview } from "./12-chat-input.js";
export const filePanelState = { open: false, activeKey: '', tabs: [] };
export const FILE_MD_NAME_RE = /\.(md|markdown|mdown)$/i;

export function filePanelTabKey(raw) {
  return String(raw || '').replace(/\\/g, '/').toLowerCase();
}

// 编辑态脚注：手机上既没有 Ctrl/⌘ 也没有 Esc（虚拟键盘敲不出来），照桌面文案就是
// 「给用户看一句做不到的提示」。判据与第 1 项同一处（`isCoarsePointer`），不另写一套。
function fileEditHint() {
  return isCoarsePointer()
    ? '点「保存」写回磁盘，「取消」放弃修改'
    : 'Ctrl/⌘ + S 或 Ctrl/⌘ + Enter 保存 · Esc 取消';
}

// 视口边界的唯一来源：CSS 的 @media (max-width: 760px) 与下面这个常量必须一致。
export const NARROW_VIEWPORT_MAX = 760;

// 文件面板能不能用，只看面板是否存在：手机上它是全屏抽屉形态，不再是「不可用」。
// （此前按视口宽度大于 760 直接判死，手机端因此整体丢失「打开文件」能力。）
export function filePanelUsable() {
  return !!$('#filePanel');
}

export function filePanelWidthPx() {
  const saved = parseFloat(localStorage.getItem('naibaChatFilePanelW') || ''); const max = Math.max(300, Math.floor(window.innerWidth * 0.5));
  return Math.max(280, Math.min(max, Number.isFinite(saved) ? saved : Math.round(window.innerWidth * 0.36)));
}

export function applyFilePanelOpenClass() {
  const shell = $('#appShell');
  if (!shell) return;
  shell.classList.toggle('file-panel-open', filePanelState.open);
  if (filePanelState.open) shell.style.setProperty('--file-panel-w', `${filePanelWidthPx()}px`);
}

export function openFilePanel(rawPath) {
  if (!filePanelUsable()) return false; // 面板不存在（理论上不会）：交给消息里的文件摘要兜底
  if (!rawPath) return false;
  if (!state.conversationId) {
    toast('请先打开一个会话');
    return false;
  }
  filePanelState.open = true;
  applyFilePanelOpenClass();
  const tab = ensureFileTab(rawPath, true);
  renderFilePanel();
  // 一律强制重读：AI 可能在本轮/上一轮改过该文件，缓存的 tab.info 不能当作最新内容（见 reloadFileTab）。
  reloadFileTab(tab);
  return true;
}

export function closeFilePanel(clearTabs = false) {
  filePanelState.open = false;
  if (clearTabs) {
    filePanelState.tabs = [];
    filePanelState.activeKey = '';
  } else if (!filePanelState.tabs.some((item) => item.key === filePanelState.activeKey)) {
    // Keep the most recently opened file visible when the panel is reopened.
    filePanelState.activeKey = filePanelState.tabs[0]?.key || '';
  }
  applyFilePanelOpenClass();
  renderFilePanel();
}

export function ensureFileTab(rawPath, activate = false) {
  const key = filePanelTabKey(rawPath);
  let tab = filePanelState.tabs.find((item) => item.key === key);
  if (!tab) {
    const name = String(rawPath).replace(/\\/g, '/').split('/').pop() || rawPath;
    tab = {
      key,
      raw: String(rawPath),
      name,
      info: null,
      loading: false,
      reloading: false, // 已有内容时的后台重读（保留旧内容渲染，不闪空态）
      refreshedAt: 0,   // 上次重读的发起时间，供 reloadFileTab 做节流
      error: '',
      editing: false,
      draft: null,
    };
    filePanelState.tabs.push(tab);
  }
  if (activate) filePanelState.activeKey = key;
  return tab;
}

export function activeFileTab() {
  return filePanelState.tabs.find((item) => item.key === filePanelState.activeKey) || null;
}

// 同一标签在窗口内的重复点击（点 chip / 切标签 / 重开面板）只发一次请求，避免内容抖动。
export const FILE_RELOAD_THROTTLE_MS = 400;

/**
 * 读取文件内容。force=true 时忽略已缓存的 tab.info，重新从磁盘读取：
 * - 有旧内容：请求期间保留旧内容渲染，成功后整体替换（不闪「文件尚未加载」空态）；
 * - 读取失败：有旧内容则保留旧内容并 toast，无旧内容才落 tab.error；
 * - 成功后重置 editing/draft（脏草稿的拦截在 reloadFileTab，不在这里）。
 */
export async function loadFileTab(tab, force = false) {
  if (!tab || tab.loading || (tab.info && !force)) return;
  const hadInfo = Boolean(tab.info);
  tab.loading = true;
  if (hadInfo) tab.reloading = true;
  else tab.error = '';
  renderFilePanel();
  try {
    const info = await api(`/api/conversations/${encodeURIComponent(state.conversationId)}/file/open?path=${encodeURIComponent(tab.raw)}`);
    tab.info = info;
    tab.draft = null;
    tab.editing = false;
    tab.error = '';
  } catch (error) {
    const message = error.message || '读取文件失败';
    if (hadInfo) toast(`重新读取失败，仍显示上次内容：${message}`);
    else tab.error = message;
  } finally {
    tab.loading = false;
    tab.reloading = false;
    renderFilePanel();
  }
}

/**
 * 「点击即重读」的统一入口（openFilePanel / activateFileTab / reopenFilePanel 共用）：
 * 正在编辑且草稿有改动时只提示、不覆盖用户输入；其余情况按 FILE_RELOAD_THROTTLE_MS 节流后强制重读。
 */
export function reloadFileTab(tab) {
  if (!tab) return;
  if (tab.editing && tab.draft !== null && tab.draft !== tab.info?.content) {
    toast('文件正在编辑中，已保留你的修改');
    return;
  }
  const now = Date.now();
  // 首次加载（无缓存）不受节流限制，保证点开即有内容；已有内容时窗口内的重复点击并入上一次请求。
  if (tab.info && now - (tab.refreshedAt || 0) < FILE_RELOAD_THROTTLE_MS) return;
  tab.refreshedAt = now;
  void loadFileTab(tab, true);
}

export function activateFileTab(key) {
  const tab = filePanelState.tabs.find((item) => item.key === key);
  if (!tab) return;
  filePanelState.activeKey = key;
  renderFilePanel();
  reloadFileTab(tab);
}

export function removeFileTab(key) {
  const index = filePanelState.tabs.findIndex((item) => item.key === key);
  if (index < 0) return;
  filePanelState.tabs.splice(index, 1);
  if (filePanelState.activeKey === key) {
    const next = filePanelState.tabs[index] || filePanelState.tabs[index - 1] || null;
    filePanelState.activeKey = next ? next.key : '';
  }
  if (!filePanelState.tabs.length) filePanelState.open = false;
  renderFilePanel();
}

export function renderFilePanel() {
  const panel = $('#filePanel');
  if (!panel) return;
  applyFilePanelOpenClass();
  renderFilePanelTabs();
  renderFilePanelBody();
  updateFileTabsButton();
}

export function renderFilePanelTabs() {
  const tabsEl = $('#fileTabs');
  if (!tabsEl) return;
  if (!filePanelState.tabs.length) {
    tabsEl.innerHTML = '';
    return;
  }
  tabsEl.innerHTML = filePanelState.tabs.map((tab) => {
    const dirty = tab.editing && tab.draft !== null && tab.draft !== tab.info?.content;
    const active = tab.key === filePanelState.activeKey;
    return `<span class="file-tab${active ? ' active' : ''}${dirty ? ' file-tab-dirty' : ''}" role="tab" aria-selected="${active}" data-file-tab="${escapeHtml(tab.key)}" title="${escapeHtml(tab.raw)}"><span class="file-tab-name">${escapeHtml(tab.name)}</span><button type="button" class="file-tab-x" data-file-tab-close="${escapeHtml(tab.key)}" aria-label="关闭 ${escapeHtml(tab.name)}" tabindex="-1"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button></span>`;
  }).join('');
  const activeEl = tabsEl.querySelector('[data-file-tab].active');
  if (activeEl && typeof activeEl.scrollIntoView === 'function') activeEl.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

export function fileToolbarMeta(tab) {
  const info = tab.info || {};
  const parts = [];
  if (info.kind === 'image') parts.push('图片');
  else if (info.kind === 'binary') parts.push('二进制');
  else if (tab.editing) parts.push('编辑中');
  else parts.push(FILE_MD_NAME_RE.test(info.name || '') ? 'Markdown' : '文本');
  if (info.size != null) parts.push(formatFileSize(info.size));
  if (info.truncated) parts.push('仅预览前 2MB');
  if (tab.reloading) parts.push('刷新中…');
  return parts.join(' · ');
}

export function renderFilePanelBody() {
  const body = $('#filePanelBody');
  if (!body) return;
  if (!filePanelState.open) {
    body.innerHTML = '';
    return;
  }
  const tab = activeFileTab();
  if (!tab) {
    body.innerHTML = '<div class="file-panel-hint">点击消息末尾「本轮修改文件」中的文件名，在右侧查看文件内容。<br>Markdown / 文本可手动编辑并保存回磁盘。</div>';
    return;
  }
  const info = tab.info;
  // 强制重读时旧内容仍在 info 里：继续渲染旧内容 + 工具栏「刷新中…」，不闪空态。
  if (tab.loading && !info) {
    body.innerHTML = '<div class="file-panel-hint"><div class="file-loading">正在读取文件…</div></div>';
    return;
  }
  if (tab.error && !info) {
    body.innerHTML = `<div class="file-panel-hint">无法读取文件：${escapeHtml(tab.error)}</div>`;
    return;
  }
  if (!info) {
    body.innerHTML = '<div class="file-panel-hint">文件尚未加载。</div>';
    return;
  }
  const savableText = info.kind === 'text' && info.savable && !info.truncated && !tab.editing;
  const editHtml = savableText
    ? '<button type="button" class="control-button" data-file-edit>编辑</button>'
    : '';
  // 「下载」是**独立**于编辑态的动作：手机上它是把已生成/已修改的文件存到本地的唯一入口
  // （此前面板工具栏只有「编辑」，点开文件只能内联看，没有任何保存路径，见 §九.135 第 3 项）。
  const downloadHtml = (info.path || tab.raw) && !tab.editing
    ? '<button type="button" class="control-button" data-file-download title="保存到本地">下载</button>'
    : '';
  const actionHtml = editHtml + downloadHtml;
  const truncNote = info.truncated ? '<small>（截断）</small>' : '';
  const toolbarHtml = `
      <div class="file-view-toolbar">
        <div class="file-view-title">
          <b title="${escapeHtml(info.path || tab.raw)}">${escapeHtml(info.name || tab.name)}${truncNote}</b>
          <small>${escapeHtml(fileToolbarMeta(tab))}${info.path ? ` · ${escapeHtml(info.path)}` : ''}</small>
        </div>
        <div class="file-view-actions">${actionHtml}</div>
      </div>`;
  // 编辑中不重建编辑区（§九.135 第 2 项）：`body.innerHTML = ...` 会把 textarea 连根拔掉，
  // 选区、光标、正在显示的系统「复制/粘贴」菜单一起消失 —— 手机上"编辑文件很奇怪"的头号成因。
  // 只把工具栏换掉（文件名/状态/动作都可能变），编辑区 DOM 原样留着；textarea 是活节点，
  // 高度与滚动位置也就跟着保住了。
  const live = body.querySelector('.file-view');
  const keepEditor = Boolean(tab.editing && live
    && live.dataset.fileKey === tab.key && live.querySelector('.file-edit-textarea'));
  if (keepEditor) {
    const bar = live.querySelector('.file-view-toolbar');
    if (bar) bar.outerHTML = toolbarHtml;
    return;
  }
  body.innerHTML = `
    <div class="file-view${tab.editing ? ' is-editing' : ''}" data-file-key="${escapeHtml(tab.key)}">${toolbarHtml}
      ${fileContentViewHtml(tab)}
    </div>`;
  if (tab.editing) {
    const textarea = body.querySelector('.file-edit-textarea');
    if (textarea && !textarea.dataset.bound) {
      // 只在**新建** textarea 时挂一次监听：编辑区现在会在重绘之间存活，
      // 沿用"每次 render 都重挂"的写法会同一节点上叠出多份监听（保存会连着触发好几次）。
      textarea.dataset.bound = '1';
      textarea.focus();
      textarea.addEventListener('input', () => { tab.draft = textarea.value; });
      textarea.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); cancelFileEdit(tab.key); }
        else if ((event.ctrlKey || event.metaKey) && (event.key === 's' || event.key === 'S')) { event.preventDefault(); saveFileTab(tab.key); }
        else if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); saveFileTab(tab.key); }
      });
    }
  }
}

export function fileContentViewHtml(tab) {
  const info = tab.info || {};
  if (tab.editing) {
    const value = tab.draft !== null && tab.draft !== undefined ? tab.draft : (info.content || '');
    // 「保存 / 取消」放在编辑区**脚底**，不再挂在顶部工具栏（§九.135 第 2 项）：
    // 编辑区是 flex 列（textarea 撑开 + 这一条钉在底部），面板在手机上是 `inset: 0` 的全屏抽屉，
    // 键盘弹起时面板跟着视觉视口收缩 ⇒ 这一条自然浮在键盘上方。此前按钮在顶部工具栏，
    // 键盘一弹就被挤出可视区，手机上"改完存不了"。
    return `<div class="file-edit-area"><textarea class="file-edit-textarea" spellcheck="false" aria-label="编辑 ${escapeHtml(info.name || '')}">${escapeHtml(value)}</textarea><div class="file-edit-foot"><span class="file-edit-hint">${escapeHtml(fileEditHint())}</span><button type="button" class="control-button" data-file-edit-cancel>取消</button><button type="button" class="primary-button" data-file-save>保存</button></div></div>`;
  }
  if (info.kind === 'image') {
    const url = convFileRawUrl(info.path || tab.raw, fileVersionToken(info));
    return `<div class="file-image-wrap"><img src="${escapeHtml(url)}" alt="${escapeHtml(info.name || '')}" data-large-url="${escapeHtml(url)}"></div>`;
  }
  if (info.kind === 'binary') {
    return `<div class="file-binary-note"><p>这是二进制文件，无法在此预览。</p><p>大小：${escapeHtml(formatFileSize(info.size))}${info.path ? ` · <code>${escapeHtml(info.path)}</code>` : ''}</p></div>`;
  }
  const text = String(info.content || '');
  if (FILE_MD_NAME_RE.test(info.name || '') && !info.truncated) {
    // 文件预览始终按纯文本/Markdown 规则渲染，不受对话富文本开关影响。
    return `<div class="file-preview-md message-body answer-content">${markdownFilePreview(text)}</div>`;
  }
  return `<pre class="file-preview-text">${escapeHtml(text)}</pre>`;
}

/** 图片版本令牌：mtime-size。同名文件被覆盖后令牌变化 → URL 变化 → 绕过浏览器缓存（/file/raw 带 max-age）。 */
export function fileVersionToken(info) {
  const mtime = Number(info?.mtime);
  const size = Number(info?.size);
  if (!Number.isFinite(mtime) && !Number.isFinite(size)) return '';
  return `${Number.isFinite(mtime) ? mtime : ''}-${Number.isFinite(size) ? size : ''}`;
}

export function convFileRawUrl(path, version = '') {
  const base = `/api/conversations/${encodeURIComponent(state.conversationId)}/file/raw?token=${encodeURIComponent(state.token)}&path=${encodeURIComponent(String(path || ''))}`;
  return version ? `${base}&v=${encodeURIComponent(version)}` : base;
}

export function startFileEdit(key) {
  const tab = filePanelState.tabs.find((item) => item.key === key);
  if (!tab || !tab.info || tab.info.kind !== 'text' || !tab.info.savable || tab.info.truncated) {
    toast('该文件不可编辑（仅支持编辑本会话改动过、工作区内且未截断的文本文件）');
    return;
  }
  tab.editing = true;
  tab.draft = tab.info.content || '';
  renderFilePanel();
}

export function cancelFileEdit(key) {
  const tab = filePanelState.tabs.find((item) => item.key === key);
  if (!tab) return;
  tab.editing = false;
  tab.draft = null;
  renderFilePanel();
}

export async function saveFileTab(key) {
  const tab = filePanelState.tabs.find((item) => item.key === key);
  if (!tab || !tab.info) return;
  const textarea = $('#filePanelBody .file-edit-textarea');
  const content = textarea ? textarea.value : (tab.draft !== null ? tab.draft : tab.info.content);
  const targetPath = tab.raw;
  try {
    await api(`/api/conversations/${encodeURIComponent(state.conversationId)}/file/save`, {
      method: 'POST',
      body: { path: targetPath, content },
    });
    toast(`已保存 ${tab.name}`);
    tab.draft = null;
    tab.editing = false;
    await loadFileTab(tab, true); // 强制重读刷新 content/mtime；旧内容继续渲染，不闪空态
  } catch (error) {
    toast(`保存失败：${error.message}`);
  }
}

export function formatFileSize(bytes) {
  const size = Number(bytes || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function openSidebar() {
  $('#sidebar').classList.add('open');
  $('#sidebarBackdrop').classList.add('open');
}

export function closeSidebar() {
  $('#sidebar').classList.remove('open');
  $('#sidebarBackdrop').classList.remove('open');
}

// ---- 左右侧栏折叠 / 展开（桌面端；手机端侧栏保持抽屉式开关）----
export function sidebarDesktop() {
  return window.innerWidth > NARROW_VIEWPORT_MAX;
}

export function setLeftSidebarCollapsed(collapsed) {
  const shell = $('#appShell');
  if (!shell) return;
  if (collapsed && !sidebarDesktop()) return; // 手机抽屉由 openSidebar/closeSidebar 管理
  shell.classList.toggle('sidebar-collapsed', Boolean(collapsed));
  if (collapsed) localStorage.setItem('naibaChatSidebarCollapsed', '1');
  else localStorage.removeItem('naibaChatSidebarCollapsed');
  renderSidebar();
}

export function restoreLeftSidebarCollapse() {
  const shell = $('#appShell');
  if (!shell) return;
  const collapsed = sidebarDesktop() && localStorage.getItem('naibaChatSidebarCollapsed') === '1';
  shell.classList.toggle('sidebar-collapsed', Boolean(collapsed));
}

// 文件面板重开：右侧栏收起但标签还在时，从顶栏「文件 N」重新展开
export function reopenFilePanel() {
  if (!filePanelUsable()) return;
  // 仅当已点开过文件（存在保留的标签）时顶栏按钮才出现；空会话不展示入口
  if (!filePanelState.tabs.length) return;
  filePanelState.open = true;
  applyFilePanelOpenClass();
  renderFilePanel();
  // 重开面板同样按「点击即重读」处理：期间文件可能已被 AI 改动。
  reloadFileTab(activeFileTab());
}

export function updateFileTabsButton() {
  const button = $('#openFileTabs');
  if (!button) return;
  const hasTabs = filePanelState.tabs.length > 0;
  // 旧逻辑：顶栏「文件 N」是"面板收起后的重开入口"——点过文件 chip 才有按钮
  const show = filePanelUsable() && !filePanelState.open && hasTabs;
  button.hidden = !show;
  button.classList.toggle('has-tabs', hasTabs);
  button.title = '重新打开文件面板（保留已打开的文件标签）';
  const count = $('#fileTabsCount');
  if (count) {
    count.textContent = hasTabs ? String(filePanelState.tabs.length) : '';
    count.hidden = !hasTabs;
  }
}

