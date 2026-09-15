// ============================================================
// 01-core.js —— 拆分自 public/app.js 第 1-397 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { resizeTextarea } from "./13-skill-refs.js";
export const urlToken = new URLSearchParams(location.search).get('token') || '';
if (urlToken) {
  localStorage.setItem('naibaChatToken', urlToken);
  history.replaceState(null, '', location.pathname);
}

export const storedSkillIds = JSON.parse(localStorage.getItem('naibaChatSkillIds') || localStorage.getItem('lanSkillIds') || '[]');
export const storedSkillMode = localStorage.getItem('naibaChatSkillMode');
export const legacyAutoSkills = localStorage.getItem('naibaChatAutoSkills') ?? localStorage.getItem('lanAutoSkills');
export const initialSkillMode = ['auto', 'pinned', 'exclusive'].includes(storedSkillMode)
  ? storedSkillMode
  : (legacyAutoSkills === 'false' && storedSkillIds.length ? 'pinned' : 'auto');

export const state = {
  token: urlToken || localStorage.getItem('naibaChatToken') || localStorage.getItem('lanSkillToken') || '',
  bootstrap: null,
  appearance: { theme: 'system', skin: 'violet' },
  conversations: [],
  conversationId: '',
  selectedSkills: storedSkillIds,
  skillMode: initialSkillMode,
  pendingFiles: [],
  // 「编辑消息」态：同一套输入区移动到被编辑的消息处，文字与附件仍使用输入区的当前值。
  // 同一时刻最多编辑一条消息；底部草稿由编辑入口暂存，退出后恢复。
  editingMessageId: '',
  editComposerStash: null,
  abortController: null,
  chatRunId: '',
  runConversationId: '',
  runSequence: 0,
  runEvents: {},
  runRow: null,
  runReconnectTimers: new Set(),
  cancelRequested: false,
  cancelConversationId: '',
  cancelledRunIds: new Set(),
  // Run 过程看护/重连状态
  runGeneration: 0,        // 每次重建流自增，旧代回调一律丢弃，防竞态覆盖
  runAttempt: 0,           // 本次连接生命周期内的重连次数（指数退避用）
  runReconnectAt: 0,       // 重连冷却截止时间戳；0 表示无需冷却
  runLastActivityAt: 0,    // 事件流最近一次活跃时间戳（含 heartbeat，用于看门狗判死）
  runContentActivityAt: 0, // 最近一次真实内容事件时间戳（不含 heartbeat，用于“等待中”计时）
  runWatchdogTimer: null,  // 看门狗定时器句柄
  runWaitTimer: null,      // “无进展等待”轻量计时器句柄（每 1s）
  runWaitShown: false,     // 当前是否正在显示“等待中 · 已等待 X 秒”
  runWaitPrevText: '',     // 显示等待前的 #runtimeStatus 原文，用于恢复
  runProbeMisses: 0,       // 看门狗连续判定空闲计数
  runRecovering: false,    // 防止 看门狗/轮询 双触发重连的互斥锁
  connectionState: 'connected', // 'connected' | 'reconnecting'（去重角标依据）
  checkRunEligible: false, // 是否处于"等待轮询兜底恢复"的状态
  elapsedTimer: null,      // “已等待 X 秒”计时器句柄
  elapsedBase: '',
  elapsedSince: 0,
  taskSubmitting: false,
  renameConversationId: '',
  newWorkspaceDir: '',
  providerEditing: false,
  providerKindTab: 'online',
  syncTimer: null,
  syncInFlight: false,
  syncPolling: false,
  updatePollTimer: null,
  conversationSnapshot: '',
  // 懒加载：当前会话的完整消息数组（渲染窗口只取其中一段）+ 窗口起点（消息下标，恒为轮起点）
  // + 这份数据属于哪个会话（切换会话要重置窗口，同会话刷新要保留窗口与滚动位置）。
  messages: [],
  renderStart: 0,
  messagesConversationId: '',
  // 首轮上下文（系统提示词 + 工具集）折叠卡数据；切换会话时由 openConversation 拉取
  firstTurnInfo: null,
  agentFormSkillIds: [],
  agentFormToolScope: [],
  agentFormIsNew: false,
  // 旧配置兼容用：当前工具目录里已不存在的工具名（如已移除的 activate_skill、
  // 临时掉线的 MCP 工具）。原样保留、单独展示，不参与勾选/计数/预设匹配，
  // 保存时随已知工具一起写回，避免用户重配。
  agentFormUnknownTools: [],
  // 旧 Agent 的 tool_scope 为空 = 不限制（运行时全放行，且新工具自动纳入）。
  // 界面上按“全选”展示，但只要用户没动过就仍以空数组保存，避免被固化成死列表。
  agentFormUnrestricted: false,
  agentFormScopeTouched: false,
  // 工具集搜索框关键词（Agent 表单打开时复位）：非空时工具列表切成平铺搜索结果视图。
  agentToolFilter: '',
  // 「我的工具集」：后端 config.json 的 tool_sets（bootstrap 带回、保存/删除后刷新），
  // 不再走 localStorage——冻结版 pywebview private_mode 会清空 localStorage。
  toolTemplates: [],
  toolCatalog: null,
  // 工具目录拉取时间戳（短时效缓存：MCP 按需连接，启动时的目录可能还没有 mcp__* 工具）。
  toolCatalogAt: 0,
  tasks: [],
  // 任务面板的数据新鲜度：轮询失败时面板要如实显示原因与「最后成功更新」时间，
  // 否则用户看到的是上一次的旧状态却以为是最新的。
  taskSyncedAt: 0,
  taskSyncFailed: '',
  taskTimer: null,
  taskPollInFlight: false,
  taskPolling: false,
  mcpPollInFlight: false,
  mcpPolling: false,
  mcpPollTimer: null,
  visionTimer: null,
  visionStartedAt: 0,
  webSearchEnabled: false,
  deepReasoningEnabled: false,
  reasoningEffort: 'auto',
  contextUsage: null,
  // 最近一次渲染出的上下文占用百分比（0 = 未知/无上限）：发送前提醒判定用。
  contextPercent: 0,
  // 实时圆环所属会话：运行中历史重渲染不得覆盖实时值（见 updateContextUsage）。
  contextUsageConversationId: '',
  // 上下文提醒：上次提醒时的占用百分比（0 = 本会话尚未提醒）。再涨 5% 会再次提醒。
  contextWarningAtPercent: 0,
  contextWarningConversationId: '',
  providerModelCapabilities: {},
  // 已按 API profile 缓存的可用模型目录；会话模型下拉只允许使用这里的结果。
  providerModelCatalogs: {},
  workspaces: [],
  workspaceSort: 'updated',
  workspaceSearch: '',
  expandedGroups: new Set(),
  customPrompts: [],
  editingStarterPrompt: -1,
  // 编辑弹窗的目标列表：'starter'（开始页自定义指令）/ 'quick'（会话内快捷消息）
  editingPromptTarget: 'starter',
  conversationPromptPresets: [],
  // 「存为/编辑快捷提示词」弹窗正在编辑的预设 id（空=另存为新条目）。
  agentPromptPresetEditingId: '',
  // 工具集编辑态：正在编辑的「我的工具集」id（空=新建）。
  agentToolEditingId: '',
  // A fresh update check should immediately surface a newer release in the
  // closed select; user choices made afterwards must still be preserved.
  updateAutoSelectLatest: false,
};
export const draggedFileCache = new Map();

// ---- 外观主题 ----
// 主题偏好同时保存在服务端 settings.appearance 与本地缓存：本地缓存用于首屏无闪烁，
// 服务端值在 bootstrap 返回后覆盖它，从而在同一实例的桌面/手机端保持一致。
const APPEARANCE_KEY = 'naibaChatAppearance';
const THEMES = new Set(['system', 'light', 'dark']);
const SKINS = new Set(['violet', 'ocean', 'rose', 'forest']);

function readStoredAppearance() {
  try {
    const raw = JSON.parse(localStorage.getItem(APPEARANCE_KEY) || '{}');
    return {
      theme: THEMES.has(raw.theme) ? raw.theme : 'system',
      skin: SKINS.has(raw.skin) ? raw.skin : 'violet',
    };
  } catch (_) {
    return { theme: 'system', skin: 'violet' };
  }
}

function resolvedTheme(theme) {
  return theme === 'system'
    ? (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : theme;
}

export function applyAppearance(appearance = {}) {
  const previous = state.appearance || {};
  const next = {
    theme: THEMES.has(appearance.theme) ? appearance.theme : (THEMES.has(previous.theme) ? previous.theme : 'system'),
    skin: SKINS.has(appearance.skin) ? appearance.skin : (SKINS.has(previous.skin) ? previous.skin : 'violet'),
  };
  state.appearance = next;
  const root = document.documentElement;
  const actualTheme = resolvedTheme(next.theme);
  root.dataset.theme = actualTheme;
  root.dataset.themeMode = next.theme;
  root.dataset.skin = next.skin;
  root.style.colorScheme = actualTheme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = actualTheme === 'dark' ? '#111522' : (next.skin === 'ocean' ? '#f0f7ff' : '#f4f5f2');
  try { localStorage.setItem(APPEARANCE_KEY, JSON.stringify(next)); } catch (_) { /* storage disabled */ }
  return next;
}

export function initializeAppearance() {
  const initial = readStoredAppearance();
  applyAppearance(initial);
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  media?.addEventListener?.('change', () => {
    if (state.appearance?.theme === 'system') applyAppearance(state.appearance);
  });
  return initial;
}

// 在 bootstrap 完成后调用，服务端配置优先；旧版本无 appearance 时保留本地偏好。
export function syncAppearanceFromBootstrap(bootstrap) {
  const configured = bootstrap?.settings?.appearance || bootstrap?.appearance;
  const local = readStoredAppearance();
  return applyAppearance({
    theme: configured?.theme ?? local.theme,
    skin: configured?.skin ?? local.skin,
  });
}

export async function saveAppearance(patch = {}) {
  const next = applyAppearance({ ...state.appearance, ...patch });
  try {
    const result = await api('/api/settings', { method: 'POST', body: { appearance: next } });
    const saved = result?.settings?.appearance || result?.appearance;
    if (saved) applyAppearance(saved);
    if (state.bootstrap?.settings) state.bootstrap.settings.appearance = saved || next;
    return state.appearance;
  } catch (error) {
    // 乐观更新后端失败时仍保留本地选择，调用方负责提示用户。
    throw error;
  }
}

export const $ = (selector) => document.querySelector(selector);
export const $$ = (selector) => [...document.querySelectorAll(selector)];

export const emptyStateElement = $('#emptyState');

// 侧栏底部唯一的状态指示灯（#serverDot）：绿=服务已建立，红=连不上服务端。
// 只在网络层失败（服务端没开/端口不通）时亮红；HTTP 4xx/5xx 说明服务在线，仍是绿。
export function setServerStatus(ok) {
  const dot = $('#serverDot');
  if (!dot) return;
  const connected = Boolean(ok);
  dot.classList.toggle('connected', connected);
  dot.classList.toggle('error', !connected);
  const label = connected ? '服务已连接' : '无法连接服务端';
  dot.title = label;
  dot.setAttribute('aria-label', label);
}

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body && typeof options.body !== 'string' && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (error) {
    setServerStatus(false);
    throw error;
  }
  setServerStatus(true);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

export function toast(message) {
  const element = $('#toast');
  // 模态 <dialog> 在浏览器 top layer：body 上的 fixed 浮层（哪怕 z-index 再高）都会被整块盖住，
  // 表现为"在设置/Agent 弹层里点按钮，底部提示看不见"。与右键菜单同一解法（§九.50）：
  // 有模态弹层时把 toast 挂进该弹层内部（fixed 定位不受祖先 overflow 裁剪），没有则回到 body。
  const container = topLayerContainer();
  if (element.parentElement !== container) container.append(element);
  element.textContent = message;
  if (typeof element.show === 'function' && !element.open) {
    element.show();
  }
  element.classList.remove('show');
  // 强制一次重排再显示，确保每次都能播放淡入动画
  void element.offsetWidth;
  element.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    element.classList.remove('show');
    if (typeof element.close === 'function' && element.open) {
      element.close();
    }
  }, 2200);
}

export async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (_) {
      // WebView and LAN HTTP pages may not grant the Clipboard API permission.
    }
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('浏览器未允许访问剪贴板');
}

export let contextMenuSelection = '';
export let contextMenuPreviousFocus = null;
export let contextMenuMode = 'selection'; // 'selection' | 'edit'
export let contextMenuTarget = null;
export let contextMenuRangeStart = 0;
export let contextMenuRangeEnd = 0;

export function editableElement(target) {
  if (!(target instanceof Element)) return null;
  const editable = target.closest('textarea, input, [contenteditable="true"]');
  if (!editable) return null;
  if (editable instanceof HTMLInputElement
      && ['button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit'].includes(editable.type)) {
    return null;
  }
  return editable;
}

export function ensureContextMenu() {
  let menu = $('#textContextMenu');
  if (menu) return menu;
  menu = document.createElement('div');
  menu.id = 'textContextMenu';
  menu.className = 'text-context-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', '文本操作');
  menu.hidden = true;
  document.body.append(menu);
  return menu;
}

export function setContextMenuItems() {
  const menu = ensureContextMenu();
  if (contextMenuMode === 'edit') {
    menu.setAttribute('aria-label', '文本框操作');
    menu.innerHTML = `
      <button type="button" role="menuitem" data-context-action="undo">撤销</button>
      <button type="button" role="menuitem" data-context-action="redo">重做</button>
      <button type="button" role="menuitem" data-context-action="cut">剪切</button>
      <button type="button" role="menuitem" data-context-action="copy">复制</button>
      <button type="button" role="menuitem" data-context-action="paste">粘贴</button>
      <button type="button" role="menuitem" data-context-action="delete">删除</button>
      <button type="button" role="menuitem" data-context-action="select-all">全选</button>`;
  } else {
    menu.setAttribute('aria-label', '选中文本操作');
    menu.innerHTML = `
      <button type="button" role="menuitem" data-context-action="copy">复制选中</button>
      <button type="button" role="menuitem" data-context-action="quote">快速发送</button>`;
  }
}

export function hideTextContextMenu() {
  const menu = $('#textContextMenu');
  if (menu) menu.hidden = true;
}

// 最上层的模态 <dialog>（浏览器 top layer）；没有则返回 body。
// 用途：模态弹层永远盖住 body 上的 fixed 元素（z-index 无效），浮层要么挂进它、要么用 popover。
// `:modal` 只匹配 showModal() 打开的对话框，可排除 toast（它是 <dialog> 但用 show()，非模态）。
export function topLayerContainer() {
  const open = [...document.querySelectorAll('dialog[open]')];
  const modals = open.filter((dialog) => {
    try {
      return dialog.matches(':modal');
    } catch (_) {
      return !dialog.classList.contains('toast');
    }
  });
  return modals.length ? modals[modals.length - 1] : document.body;
}

export function showTextContextMenu(event, selection = '', mode = 'selection', target = null) {
  const menu = ensureContextMenu();
  contextMenuSelection = selection;
  contextMenuMode = mode;
  contextMenuTarget = target;
  contextMenuPreviousFocus = document.activeElement;
  if (target && typeof target.selectionStart === 'number') {
    contextMenuRangeStart = target.selectionStart;
    contextMenuRangeEnd = target.selectionEnd;
  } else {
    contextMenuRangeStart = 0;
    contextMenuRangeEnd = 0;
  }
  setContextMenuItems();
  // 模态弹层在 top layer：body 上的 fixed 菜单会被弹层盖住（z-index 无效），
  // 必须把菜单挂进最上层那个弹层内部；没有弹层时挂回 body。
  const container = topLayerContainer();
  if (menu.parentElement !== container) container.append(menu);
  menu.hidden = false;
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  menu.style.left = `${Math.max(6, Math.min(event.clientX, window.innerWidth - width - 6))}px`;
  menu.style.top = `${Math.max(6, Math.min(event.clientY, window.innerHeight - height - 6))}px`;
  // 不要自动聚焦菜单按钮，否则文本框会失焦，选中高亮会消失。
}

export function focusContextTarget() {
  const el = contextMenuTarget;
  if (!el) return;
  el.focus({ preventScroll: true });
  if (typeof el.setSelectionRange === 'function') {
    try {
      el.setSelectionRange(contextMenuRangeStart, contextMenuRangeEnd);
    } catch (_) { /* 忽略 */ }
  }
}

export function editableSelectedText() {
  const el = contextMenuTarget;
  if (!el) return '';
  if (typeof el.value === 'string') {
    if (typeof el.selectionStart === 'number' && typeof el.selectionEnd === 'number') {
      return el.value.substring(el.selectionStart, el.selectionEnd);
    }
    // number/email 等类型不暴露 selectionStart（恒为 null）：退化为整值复制，
    // 否则这些输入框右键「复制」永远提示"没有可复制的内容"。
    return el.value;
  }
  const sel = window.getSelection();
  return sel ? sel.toString() : '';
}

export function insertTextIntoEditable(text) {
  const el = contextMenuTarget;
  if (!el) return false;
  if (typeof el.value === 'string' && typeof el.selectionStart === 'number') {
    const start = el.selectionStart;
    const end = el.selectionEnd;
    el.value = el.value.slice(0, start) + text + el.value.slice(end);
    el.setSelectionRange(start + text.length, start + text.length);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  }
  if (el.isContentEditable) {
    el.focus();
    return document.execCommand('insertText', false, text);
  }
  return false;
}

export async function runTextContextAction(action) {
  try {
    if (contextMenuMode === 'edit') {
      if (['undo', 'redo', 'cut', 'copy', 'paste', 'delete', 'select-all'].includes(action)) {
        focusContextTarget();
      }
      if (action === 'undo') {
        document.execCommand('undo');
        toast('已撤销');
      } else if (action === 'redo') {
        document.execCommand('redo');
        toast('已重做');
      } else if (action === 'cut') {
        if (document.execCommand('cut')) toast('已剪切');
        else toast('剪切失败：浏览器未授权');
      } else if (action === 'copy') {
        const text = editableSelectedText();
        if (text) {
          await copyText(text);
          toast('已复制');
        } else {
          toast('没有可复制的内容');
        }
      } else if (action === 'paste') {
        let ok = false;
        try { ok = document.execCommand('paste'); } catch (_) { /* 忽略 */ }
        if (!ok && navigator.clipboard?.readText) {
          try {
            const text = await navigator.clipboard.readText();
            ok = insertTextIntoEditable(text);
          } catch (_) { /* 忽略 */ }
        }
        if (ok) toast('已粘贴');
        else toast('粘贴失败：浏览器未授权');
      } else if (action === 'delete') {
        const el = contextMenuTarget;
        if (el && typeof el.value === 'string' && typeof el.selectionStart === 'number') {
          if (el.selectionStart === el.selectionEnd) {
            toast('请先选择要删除的内容');
          } else {
            insertTextIntoEditable('');
            toast('已删除');
          }
        } else if (document.execCommand('delete')) {
          toast('已删除');
        } else {
          toast('删除失败');
        }
      } else if (action === 'select-all') {
        const el = contextMenuTarget;
        if (el && typeof el.select === 'function') el.select();
        else if (el && typeof el.setSelectionRange === 'function') el.setSelectionRange(0, el.value.length);
        else document.execCommand('selectAll');
      }
      return;
    }

    if (action === 'copy') {
      await copyText(contextMenuSelection);
      toast('已复制选中内容');
    } else if (action === 'quote') {
      const input = $('#messageInput');
      const quoted = `"${contextMenuSelection.trim()}"`;
      input.value = String(input.value || '').replace(/\s+$/, '') + quoted;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      resizeTextarea();
      toast('已追加到输入框');
    }
  } catch (error) {
    toast(`操作失败：${error.message}`);
  } finally {
    hideTextContextMenu();
  }
}

// 程序化改输入框（快捷消息 / 技能引用 / @ 引用 / 选择按钮 / 编辑回填…）不会触发 input 事件，
// 因此发送按钮可用性等"单点写入"不会刷新。凡是以代码写 `#messageInput.value` 的地方，
// 改完必须调它一次：派发合成 input 事件，让 15-bind-events 的输入管线统一处理
// （resizeTextarea / renderInputMirror / updateSkillPopup / updateFilePopup /
// updateSendButtonState——发送按钮唯一写入点，见维护说明 §九.25）。
export function notifyComposerChanged(input = null) {
  const target = input || document.querySelector('#messageInput');
  if (target && typeof target.dispatchEvent === 'function') {
    target.dispatchEvent(new Event('input', { bubbles: true }));
  }
  return target;
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}
export function restoreSafeHtml(escaped) {
  if (!String(escaped || '').includes('&lt;')) return String(escaped || '');
  const colors = new Set(['black','silver','gray','white','maroon','red','purple','fuchsia','green','lime','olive','yellow','navy','blue','teal','aqua','orange','aliceblue','transparent']);
  const decode = (s) => String(s).replace(/&quot;/g, '"').replace(/&#039;/g, "'").replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
  return String(escaped || '').replace(/&lt;!--[\s\S]*?--&gt;/gi, '').replace(/&lt;(\/?)(font|span|b|i|u|s)([\s\S]*?)&gt;/gi, (full, slash, name, raw) => {
    const tag = name.toLowerCase(); if (slash) return `</${tag}>`; const attrs = decode(raw).trim(); if (!attrs) return `<${tag}>`; if (!['font','span'].includes(tag)) return full;
    const m = attrs.match(/^color\s*=\s*["']([^"']+)["']$/i); if (!m) return full; const value = m[1].trim();
    if (!(/^#[0-9a-f]{3,8}$/i.test(value) || colors.has(value.toLowerCase()))) return full; return `<${tag} color="${escapeHtml(value)}">`;
  });
}

// 语言别名归一化：把常见标识归到同一套规则。
export function normalizeLanguage(language) {
  const lang = String(language || '').toLowerCase().trim();
  if (/^(js|javascript|jsx|mjs|cjs)$/.test(lang)) return 'js';
  if (/^(ts|typescript|tsx)$/.test(lang)) return 'ts';
  if (/^(py|python|python3)$/.test(lang)) return 'py';
  if (/^(json|json5|jsonc)$/.test(lang)) return 'json';
  if (/^(sh|bash|shell|zsh|powershell|ps1|cmd|bat)$/.test(lang)) return 'bash';
  if (/^(html|htm|xml|svg)$/.test(lang)) return 'html';
  if (/^(css|scss|less)$/.test(lang)) return 'css';
  if (/^(ya?ml)$/.test(lang)) return 'yaml';
  if (/^(java|c|cpp|csharp|cs|go|rust|rs|php|rb|ruby|swift|kt|kotlin|scala|sql)$/.test(lang)) return 'js';
  return '';
}

// ---- 手机端顶栏折叠：收起操作区，把被顶栏吃掉的高度还给会话区 ----
// 形态定义全在 CSS 的 ≤760px 块里（桌面上按钮根本不渲染、规则也不命中），JS 只负责切类与记忆选择：
// 收起 = 隐藏整条 .topbar-actions（轮次 / 文件 / 任务 / Skill / MCP / 刷新临时让位，点细条即回），
// 并把模型与 Agent 两个下拉并回第 1 行。与 setLeftSidebarCollapsed 同一套路（localStorage 记状态）。
const TOPBAR_COMPACT_KEY = 'naibaChatTopbarCompact';

export function setTopbarCompact(compact) {
  const topbar = $('.topbar');
  if (!topbar) return;
  const collapsed = Boolean(compact);
  topbar.classList.toggle('compact', collapsed);
  try {
    if (collapsed) localStorage.setItem(TOPBAR_COMPACT_KEY, '1');
    else localStorage.removeItem(TOPBAR_COMPACT_KEY);
  } catch (_error) {
    // 隐私模式 / 存储被禁：折叠本身仍要生效，只是不跨刷新保留。
  }
  const toggle = $('#toggleTopbarCompact');
  if (toggle) {
    const label = collapsed ? '展开顶栏' : '收起顶栏';
    toggle.title = label;
    toggle.setAttribute('aria-label', label);
    toggle.setAttribute('aria-expanded', String(!collapsed));
  }
}

export function restoreTopbarCompact() {
  let collapsed = false;
  try {
    collapsed = localStorage.getItem(TOPBAR_COMPACT_KEY) === '1';
  } catch (_error) {
    collapsed = false;
  }
  setTopbarCompact(collapsed);
}

// 单条组合正则 + 线性扫描：token 先 escape 再包 span，输出安全的 HTML。
