// ============================================================
// 07-models-agents.js —— 拆分自 public/app.js 第 1966-2329 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, state, toast } from "./01-core.js";
import { renderSidebar } from "./08-conversations.js";
import { renderSkills } from "./09-settings.js";
import { appendPresetSkillsToComposer } from "./13-skill-refs.js";

const UPDATE_BUSY_PHASES = ['checking', 'downloading', 'restarting'];
const UPDATE_RELEASE_URL = 'https://github.com/yc883123/cat-chat/releases';

/* Agent 说明弹层（顶栏 Agent 选择器左侧的「?」）：与 contextUsagePopover 同一模式——
   fixed 定位 + 打开时挂到 body（顶栏容器有 overflow 裁剪），内容是静态文案，无需动态生成。 */
export function positionAgentHelpPopover() {
  const popover = $('#agentHelpPopover');
  const button = $('#agentHelpButton');
  if (!popover || !button || popover.hidden) return;
  const edge = 12;
  const gap = 9;
  const buttonRect = button.getBoundingClientRect();
  const popoverRect = popover.getBoundingClientRect();
  const maxLeft = Math.max(edge, window.innerWidth - popoverRect.width - edge);
  const left = Math.min(Math.max(edge, buttonRect.left), maxLeft);
  let top = buttonRect.bottom + gap;
  if (top + popoverRect.height > window.innerHeight - edge) {
    top = Math.max(edge, buttonRect.top - popoverRect.height - gap);
  }
  popover.style.left = `${Math.round(left)}px`;
  popover.style.top = `${Math.round(top)}px`;
}

export function toggleAgentHelpPopover(event) {
  event.stopPropagation();
  const popover = $('#agentHelpPopover');
  const button = $('#agentHelpButton');
  if (!popover || !button) return;
  const open = popover.hidden;
  if (open && popover.parentElement !== document.body) document.body.appendChild(popover);
  popover.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
  if (open) positionAgentHelpPopover();
}

export function closeAgentHelpPopover() {
  const popover = $('#agentHelpPopover');
  const button = $('#agentHelpButton');
  if (!popover || popover.hidden) return;
  popover.hidden = true;
  button?.setAttribute('aria-expanded', 'false');
}

function updateVersionAllowed(release) {
  if (!release || release.installable === false) return false;
  const value = String(release.version || release.tag || '').trim().replace(/^v/i, '');
  const match = value.match(/^(\d+)\.(\d+)\.(\d+)/);
  if (!match) return false;
  const major = Number(match[1]);
  const minor = Number(match[2]);
  const patch = Number(match[3]);
  return major > 2 || (major === 2 && (minor > 0 || (minor === 0 && patch > 0)));
}

function versionLabel(release) {
  const label = release.current ? `${release.version}（当前）` : release.version;
  return release.published_at ? `${label} · ${String(release.published_at).slice(0, 10)}` : label;
}

function syncUpdateVersionCombobox(options, selectedValue, disabled) {
  const wrap = $('#updateVersionCombobox');
  const trigger = $('#updateVersionTrigger');
  const label = $('#updateVersionLabel');
  const menu = $('#updateVersionMenu');
  const native = $('#updateVersionSelect');
  if (!wrap || !trigger || !label || !menu || !native) return;
  wrap.hidden = false;
  trigger.disabled = disabled || !options.length;
  const selected = options.find((item) => item.value === selectedValue) || options[0];
  label.textContent = selected ? selected.textContent : '尚未检查';
  trigger.setAttribute('aria-expanded', menu.hidden ? 'false' : 'true');
  menu.replaceChildren(...options.map((item, index) => {
    const node = document.createElement('div');
    node.className = 'update-version-option';
    node.id = `update-version-option-${index}`;
    node.setAttribute('role', 'option');
    node.dataset.value = item.value;
    node.textContent = item.textContent;
    node.setAttribute('aria-selected', item.value === selectedValue ? 'true' : 'false');
    node.addEventListener('mousedown', (event) => event.preventDefault());
    node.addEventListener('click', () => {
      native.value = item.value;
      native.dispatchEvent(new Event('change', { bubbles: true }));
      closeUpdateVersionMenu();
    });
    return node;
  }));
}

function closeUpdateVersionMenu() {
  const menu = $('#updateVersionMenu');
  const trigger = $('#updateVersionTrigger');
  if (!menu || !trigger) return;
  menu.hidden = true;
  trigger.setAttribute('aria-expanded', 'false');
  // 顶层宿主留空即退出顶层，避免它继续盖住对话框上的其它内容。
  const host = $('#updateVersionMenuHost');
  if (host && typeof host.hidePopover === 'function' && host.matches(':popover-open')) {
    try {
      host.hidePopover();
    } catch (error) {
      /* 已关闭或浏览器拒绝：无副作用，忽略 */
    }
  }
}

/* 版本菜单 = fixed 定位 + 顶层宿主挂载，两个坑一次解决（教训 §九.27 + §九.50）。
   坑一（裁剪）：留在 #updateVersionCombobox 里会被两层祖先裁掉——`.update-status{overflow:hidden}`
   切掉一半选项，设置面板内容区又是 overflow:auto，滚动时菜单不跟手；fixed 让包含块变成视口。
   坑二（层级）：设置页本身是模态 <dialog>（top layer），body 上的 fixed 弹层同样盖不过它，
   所以挂载点取 #updateVersionMenuHost（popover 顶层宿主，见 updateVersionMenuHost()）。
   注意：fixed 只在祖先没有 transform/filter/contain 时相对视口生效——宿主用 display: contents
   不产生盒子，满足该前提。 */
const UPDATE_VERSION_MENU_GAP = 4;
// 触发按钮与视口之间至少留出的边距，避免菜单贴死屏幕边缘。
const UPDATE_VERSION_MENU_MARGIN = 12;

/* 选中菜单的挂载容器：优先设置面板里的 popover 顶层宿主（能盖住模态 dialog），
   没有（例如宿主被裁掉、浏览器不支持 popover）则退回 body。
   注意不能用 `:popover-open` 做存在性判断——未 showPopover() 时它匹配不到任何元素。 */
function updateVersionMenuHost() {
  const host = $('#updateVersionMenuHost');
  if (!host) return document.body;
  if (typeof host.showPopover !== 'function') return document.body;
  try {
    if (!host.matches(':popover-open')) host.showPopover();
  } catch (error) {
    return document.body;
  }
  return host;
}

function positionUpdateVersionMenu() {
  const menu = $('#updateVersionMenu');
  const trigger = $('#updateVersionTrigger');
  if (!menu || !trigger || menu.hidden) return;
  const box = trigger.getBoundingClientRect();
  const width = Math.max(box.width, 220);
  const viewportHeight = window.innerHeight;
  const naturalHeight = menu.offsetHeight;
  const roomBelow = viewportHeight - box.bottom - UPDATE_VERSION_MENU_GAP - UPDATE_VERSION_MENU_MARGIN;
  const roomAbove = box.top - UPDATE_VERSION_MENU_GAP - UPDATE_VERSION_MENU_MARGIN;
  // 先尽量完整放下：下方优先，下方放不下但上方能放下则翻转。
  // 两侧都放不下时（面板本身很矮或菜单很长），选空间更大的一侧贴边，
  // 并把高度压到可用空间——否则菜单会整段伸出视口，底部选项根本点不到。
  let height = naturalHeight;
  let top;
  if (naturalHeight <= roomBelow) {
    top = box.bottom + UPDATE_VERSION_MENU_GAP;
  } else if (naturalHeight <= roomAbove) {
    top = box.top - naturalHeight - UPDATE_VERSION_MENU_GAP;
  } else if (roomBelow >= roomAbove) {
    height = Math.max(0, roomBelow);
    top = box.bottom + UPDATE_VERSION_MENU_GAP;
  } else {
    height = Math.max(0, roomAbove);
    top = box.top - height - UPDATE_VERSION_MENU_GAP;
  }
  const maxLeft = Math.max(UPDATE_VERSION_MENU_MARGIN, window.innerWidth - width - UPDATE_VERSION_MENU_MARGIN);
  menu.style.position = 'fixed';
  menu.style.width = `${width}px`;
  menu.style.maxHeight = `${Math.round(height)}px`;
  menu.style.top = `${Math.round(top)}px`;
  menu.style.left = `${Math.round(Math.min(Math.max(UPDATE_VERSION_MENU_MARGIN, box.left), maxLeft))}px`;
}

function openUpdateVersionMenu() {
  const menu = $('#updateVersionMenu');
  const trigger = $('#updateVersionTrigger');
  if (!menu || !trigger || trigger.disabled) return;
  const host = updateVersionMenuHost();
  if (menu.parentElement !== host) host.appendChild(menu);
  menu.hidden = false;
  positionUpdateVersionMenu();
  trigger.setAttribute('aria-expanded', 'true');
  const selected = menu.querySelector('[aria-selected="true"]');
  selected?.scrollIntoView({ block: 'nearest' });
}

function initUpdateVersionCombobox() {
  const trigger = $('#updateVersionTrigger');
  const menu = $('#updateVersionMenu');
  const native = $('#updateVersionSelect');
  if (!trigger || !menu || !native || trigger.dataset.bound) return;
  trigger.dataset.bound = '1';
  trigger.addEventListener('click', () => (menu.hidden ? openUpdateVersionMenu() : closeUpdateVersionMenu()));
  trigger.addEventListener('keydown', (event) => {
    const items = [...menu.querySelectorAll('[role="option"]')];
    if (!items.length) return;
    let index = items.findIndex((item) => item.getAttribute('aria-selected') === 'true');
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      // preventDefault 是必需的：空间键之外，方向键默认还会滚动祖先容器，
      // 菜单现在挂在 body 下（fixed），祖先滚动会把菜单推离触发按钮。
      event.preventDefault();
      if (menu.hidden) openUpdateVersionMenu();
      index = (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      native.value = items[index].dataset.value;
      native.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
      event.preventDefault();
      menu.hidden ? openUpdateVersionMenu() : closeUpdateVersionMenu();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeUpdateVersionMenu();
    }
  });
  trigger.addEventListener('blur', () => {
    setTimeout(() => {
      // 菜单已挂到 body 下，不再是 combobox 的后代：祖先判定要同时放行菜单自身。
      const active = document.activeElement;
      if (!active?.closest('#updateVersionCombobox') && !active?.closest('#updateVersionMenu')) {
        closeUpdateVersionMenu();
      }
    }, 0);
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('#updateVersionCombobox') && !event.target.closest('#updateVersionMenu')) {
      closeUpdateVersionMenu();
    }
  });
  // 页面/面板滚动或窗口变化时重新贴合触发按钮（fixed 不会跟着祖先滚动）。
  const reposition = () => { if (!menu.hidden) positionUpdateVersionMenu(); };
  window.addEventListener('scroll', reposition, true);
  window.addEventListener('resize', reposition);
  initUpdateVersionCombobox._ready = true;
}

export function renderUpdateStatus(status) {
  const current = status.current_version || '开发版';
  $('#currentVersion').textContent = status.current_commit ? `${current} · ${status.current_commit.slice(0, 7)}` : current;
  const select = $('#updateVersionSelect');
  const releases = Array.isArray(status.releases) ? status.releases : [];
  const latestVersion = String(status.latest_version || '').trim();
  const hasNewVersion = status.phase === 'available' && latestVersion;
  $('#latestVersion').textContent = hasNewVersion
    ? latestVersion
    : (status.phase === 'current' ? `${current}（当前）`
      : (status.phase === 'checking' ? '正在检查…' : '尚未检查'));
  const previousValue = select.value;
  // 重建版本下拉：仅保留可安装项，当前版本标记为「当前」。
  const options = releases
    .filter(updateVersionAllowed)
    .map((release) => {
      const option = document.createElement('option');
      option.value = release.tag;
      option.textContent = versionLabel(release);
      return option;
    });
  select.replaceChildren(...options);
  select.disabled = options.length === 0 || UPDATE_BUSY_PHASES.includes(status.phase);
  // 检查完成后优先选中新版本，而不是保留检查前的"当前版本"。否则在不展开
  // 下拉框时看不出已经有更新。手动选择版本后仍按用户选择保留。
  const newerOption = options.find((option) => {
    const release = releases.find((item) => item.tag === option.value);
    return release && !release.current;
  });
  if (state.updateAutoSelectLatest && status.phase !== 'checking') {
    const currentOption = options.find((option) => {
      const release = releases.find((item) => item.tag === option.value);
      return release && release.current;
    });
    const latestOption = options.find((option) => {
      const release = releases.find((item) => item.tag === option.value);
      return release && release.version === latestVersion;
    });
    const preferred = status.phase === 'current'
      ? (currentOption || latestOption || options[0])
      : (latestOption || newerOption || options[0]);
    if (preferred) select.value = preferred.value;
    state.updateAutoSelectLatest = false;
  } else if (previousValue && options.some((option) => option.value === previousValue)) {
    select.value = previousValue;
  } else if (options.length > 0) {
    select.value = options[0].value;
  }
  const selectedTag = select.value;
  const selected = releases.find((release) => release.tag === selectedTag);
  initUpdateVersionCombobox();
  syncUpdateVersionCombobox(options, selectedTag, select.disabled);
  const notes = selected && Array.isArray(selected.release_notes)
    ? selected.release_notes.filter((note) => String(note || '').trim())
    : (selected && selected.release_notes ? [String(selected.release_notes)] : []);
  const notesPanel = $('#updateNotes');
  const notesList = $('#updateNotesList');
  notesList.replaceChildren(...notes.map((note) => {
    const item = document.createElement('li');
    item.textContent = note;
    return item;
  }));
  // 仅根据是否存在更新内容显示/隐藏详情；不重置用户已展开/收起状态
  notesPanel.hidden = notes.length === 0;
  const messages = {
    idle: '点「检查更新」才会去查新版本（启动不再自动检查）；不会自动安装，请手动选择版本后点击「立即更新」。',
    checking: '正在检查更新…',
    current: '当前已经是最新版本。',
    available: '发现新版本，可以立即安装。',
    downloading: '正在下载并校验更新，请勿关闭程序。',
    restarting: '更新已准备好，程序即将重启。',
    error: status.error || '检查更新失败。',
  };
  const manualUrl = status.manual_release_url || status.release_url || UPDATE_RELEASE_URL;
  $('#updateMessage').textContent = !status.supported
    ? '当前运行目录不支持自动更新，请确认它来自受支持的 Git 仓库。'
    : (messages[status.phase] || messages.idle);
  const manualDownload = status.manual_download_required || status.manual_update_available || status.phase === 'manual' || (releases.length && !options.length);
  const pending = status.pending_verification;
  if (pending && pending.pending) {
    $('#updateMessage').textContent = pending.ok
      ? `上次更新已完成并验证通过（${pending.target_version}）。`
      : `上次更新到 ${pending.target_version} 后版本校验失败，请重新检查更新或手动安装。`;
  } else if (status.mode === 'source') {
    $('#updateMessage').textContent = '源码模式不支持一键更新，请在终端中执行 git pull --ff-only origin master。';
  } else if (selected && selected.current) {
    $('#updateMessage').textContent = '当前已安装该版本，无需更新。';
  } else if (status.phase === 'available' && selected) {
    $('#updateMessage').textContent = `将安装 ${selected.version}，完成后程序自动重启。`;
  }
  if (manualDownload) {
    const message = $('#updateMessage');
    message.replaceChildren(document.createTextNode('2.0.0 及更早版本不支持自动更新，请前往 '));
    const link = document.createElement('a');
    link.href = manualUrl;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = 'GitHub Release';
    message.append(link, document.createTextNode(' 手动下载。'));
  }
  const canInstall = status.supported && status.mode !== 'source'
    && selected && !selected.current
    && !UPDATE_BUSY_PHASES.includes(status.phase);
  $('#installUpdate').hidden = !canInstall;
  $('#checkUpdate').disabled = ['checking', 'downloading', 'restarting'].includes(status.phase);
}

export async function checkUpdate() {
  const button = $('#checkUpdate');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  button.disabled = true;
  state.updateAutoSelectLatest = true;
  renderUpdateStatus({ ...(state.bootstrap.update || {}), phase: 'checking' });
  try {
    let status = await api('/api/update/check', { method: 'POST', body: {}, signal: controller.signal });
    state.bootstrap.update = status;
    renderUpdateStatus(status);
    const startedAt = Date.now();
    while (status.phase === 'checking' && Date.now() - startedAt < 30000) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      status = await api('/api/update');
      state.bootstrap.update = status;
      renderUpdateStatus(status);
    }
    if (status.phase === 'checking') {
      renderUpdateStatus({ ...status, phase: 'error', error: '检查更新超时，请稍后重试。' });
    }
  } catch (error) {
    try {
      const status = await api('/api/update');
      state.bootstrap.update = status;
      renderUpdateStatus(status.phase === 'checking'
        ? { ...status, phase: 'error', error: '检查更新超时，请稍后重试。' }
        : status);
    } catch (_) {
      renderUpdateStatus({ ...(state.bootstrap.update || {}), phase: 'error', error: error.message });
    }
  } finally {
    clearTimeout(timeout);
    button.disabled = false;
  }
}

export async function installUpdate() {
  const button = $('#installUpdate');
  const select = $('#updateVersionSelect');
  const tag = select.value;
  const status = state.bootstrap.update || {};
  const releases = Array.isArray(status.releases) ? status.releases : [];
  const selected = releases.find((release) => release.tag === tag);
  if (!selected || !updateVersionAllowed(selected)) {
    toast(selected ? '2.0.0 及更早版本请前往 GitHub Release 手动下载' : '请先选择要安装的版本');
    return;
  }
  if (!confirm(`确定要安装版本 ${selected.version} 吗？更新完成后程序将自动重启。`)) {
    return;
  }
  button.disabled = true;
  try {
    const newStatus = await api('/api/update/install', { method: 'POST', body: { tag } });
    state.bootstrap.update = newStatus;
    renderUpdateStatus(newStatus);
    toast('正在下载更新，完成后会自动重启');
  } catch (error) {
    toast(`更新失败：${error.message}`);
    button.disabled = false;
  }
}

export function populateModels() {
  const select = $('#modelSelect');
  const previous = select.value;
  const profiles = state.bootstrap.model_profiles || state.bootstrap.providers || [];
  const defaultKey = String(state.bootstrap.default_model_key || '');
  select.innerHTML = '';

  if (!profiles.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '请先在设置中添加模型';
    select.append(opt);
  } else {
    const online = profiles.filter((p) => (p.kind || 'online') === 'online');
    const local = profiles.filter((p) => p.kind === 'local');
    const group = (label, list) => {
      if (!list.length) return;
      const og = document.createElement('optgroup');
      og.label = label;
      list.forEach((p) => {
        const option = document.createElement('option');
        option.value = p.model_key;
        option.textContent = p.name || p.model_key;
        og.append(option);
      });
      select.append(og);
    };
    group('在线 API', online);
    group('本地模型', local);
  }

  if ([...select.options].some((o) => o.value === previous)) {
    select.value = previous;
  } else if (defaultKey && [...select.options].some((o) => o.value === defaultKey)) {
    select.value = defaultKey;
  } else if (select.options.length) {
    select.selectedIndex = 0;
  }
  updateUnloadModelButton();
  void populateComposerModels();
}

export function selectedProvider() {
  const value = $('#modelSelect')?.value || '';
  if (!value) return null;
  const profiles = state.bootstrap.model_profiles || state.bootstrap.providers || [];
  return profiles.find((p) => p.model_key === value) || null;
}

// 「重新检测模型」伪选项：它只是下拉框里的一个触发器，**绝不落库、绝不发给模型**。
const COMPOSER_MODEL_REFRESH = '__refresh_composer_models__';
// 下拉框最后一次有效选择（'' = 未选择）：刷新前记下，强制重拉目录后还原。
let composerModelLastChoice = '';
let composerModelRefreshBusy = false;
// 输入区模型「可搜索下拉」浮层的状态（原生 select 仍是唯一值来源，这里只是它的投影）。
export const composerPickerState = { open: false, activeIndex: -1, items: [] };

export function composerModelChoice() {
  const select = $('#composerModelSelect');
  if (!select) return '';
  const value = String(select.value || '').trim();
  return value === COMPOSER_MODEL_REFRESH ? '' : value;
}

function composerModelEntries(provider) {
  const entries = [];
  const seen = new Set();
  const add = (id, name = id) => {
    const clean = String(id || '').trim();
    if (!clean || seen.has(clean)) return;
    seen.add(clean);
    entries.push({ id: clean, name: String(name || clean) });
  };
  (state.providerModelCatalogs[provider?.model_key] || []).forEach((model) => {
    add(model?.id, model?.name || model?.id);
  });
  return entries;
}

function renderComposerModels(provider, preferredModel = '') {
  const select = $('#composerModelSelect');
  if (!select) return;
  const desired = String(preferredModel || '').trim();
  const entries = composerModelEntries(provider);
  select.replaceChildren();
  const savedMissing = Boolean(desired) && !entries.some((item) => item.id === desired);
  // 会话没有明确选择时给一个占位项，**不再默认选中目录第一项**——目录顺序由供应商
  // `/v1/models` 的返回顺序决定，没有任何语义（teynex 实测把最弱那个排第一）。
  if (savedMissing) {
    const saved = document.createElement('option');
    saved.value = desired;
    saved.textContent = `已保存：${desired}（请重新检查模型）`;
    select.append(saved);
  } else if (!desired) {
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = entries.length ? '请选择模型…' : '请先在设置中检查模型';
    placeholder.disabled = true;
    select.append(placeholder);
  }
  entries.forEach((model) => {
    const option = document.createElement('option');
    option.value = model.id;
    option.textContent = model.name;
    select.append(option);
  });
  // 会话内直接重拉目录的入口：供应商新上架的模型不必再进设置页点「检查模型」。
  const refresh = document.createElement('option');
  refresh.value = COMPOSER_MODEL_REFRESH;
  refresh.textContent = '↻ 重新检测模型';
  select.append(refresh);
  const matched = Boolean(desired) && [...select.options].some((option) => option.value === desired);
  select.value = matched ? desired : '';
  composerModelLastChoice = select.value;
  // 目录/会话/API 变了：触发按钮文案与浮层候选一起对齐到新的 options。
  syncComposerModelPicker();
}

export function composerModelIsValidated() {
  const provider = selectedProvider();
  const choice = composerModelChoice();
  const catalog = provider?.model_key ? state.providerModelCatalogs[provider.model_key] : null;
  return Boolean(choice && Array.isArray(catalog)
    && catalog.some((model) => String(model?.id || '').trim() === choice));
}

export async function populateComposerModels(preferredModel = null, { force = false } = {}) {
  const provider = selectedProvider();
  const providerKey = String(provider?.model_key || '');
  // 记下发请求时的会话：目录返回后会话可能已经切走。两个会话共用同一 API 时，
  // 先返回的旧请求若照旧重绘，会把旧会话的模型顶到新会话的下拉框上。
  const conversationId = state.conversationId;
  // null = 未指定：保持当前下拉框选择；字符串则按它重绘。
  const desired = preferredModel === null
    ? composerModelChoice()
    : String(preferredModel || '').trim();
  renderComposerModels(provider, desired);
  if (!providerKey) {
    if (force) toast('请先在设置页配置 API 供应商，再重新检测模型');
    return;
  }
  // 目录缓存判据是「**有没有模型**」而不是「有没有查过」：供应商返回 200 但列表为空
  // （本地模型还没 pull/加载）时不能被当成已缓存，否则本页会永久卡在「请先在设置中
  // 检查模型」，模型后来上架了也刷不出来。force = 用户点了「↻ 重新检测模型」。
  if (!force && (state.providerModelCatalogs[providerKey] || []).length) return;
  try {
    const result = await api('/api/providers/models', { method: 'POST', body: { model_key: providerKey } });
    state.providerModelCatalogs[providerKey] = Array.isArray(result.models) ? result.models : [];
    // 目录本身只写缓存；仅当「API 未变 且 会话未变」时才重绘，且选中项一律按
    // 当前下拉框的实际值重算，不再回填旧请求的 preferredModel。
    if ($('#modelSelect')?.value !== providerKey) return;
    if (state.conversationId !== conversationId) return;
    renderComposerModels(selectedProvider(), composerModelChoice());
    if (force) {
      const count = state.providerModelCatalogs[providerKey].length;
      toast(count ? `已重新检测到 ${count} 个模型` : '该 API 当前没有可用模型');
    }
  } catch (error) {
    // 用户主动点「重新检测模型」时必须出声，不能只藏在控制台里。
    if (force) toast(`无法获取模型目录：${error.message}`);
    else console.debug('[naiba] 获取 API 模型目录失败:', error.message);
  }
}

async function persistConversationModelName(modelName) {
  if (!state.conversationId) return;
  const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
    method: 'POST', body: { model_name: modelName },
  });
  const index = state.conversations.findIndex((item) => item.id === state.conversationId);
  if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
}

// 「↻ 重新检测模型」：强制重拉目录（绕过缓存），完成后还原刷新前的选择。
async function refreshComposerModels() {
  const select = $('#composerModelSelect');
  if (!select || composerModelRefreshBusy) return;
  const keep = composerModelLastChoice;
  composerModelRefreshBusy = true;
  select.disabled = true;
  try {
    await populateComposerModels(keep, { force: true });
  } finally {
    composerModelRefreshBusy = false;
    select.disabled = false;
    // 触发按钮的可用态跟着 select 走：刷新结束要恢复可点。
    syncComposerModelPicker();
  }
}

export async function saveComposerModelSelection() {
  const select = $('#composerModelSelect');
  if (!select) return;
  // 刷新项是动作不是选择：不进「已保存的会话模型」。
  if (select.value === COMPOSER_MODEL_REFRESH) {
    await refreshComposerModels();
    return;
  }
  composerModelLastChoice = composerModelChoice();
  try {
    await persistConversationModelName(composerModelChoice());
  } catch (error) {
    toast(`保存模型失败：${error.message}`);
  }
}

// ---- 输入区模型「可搜索下拉」 -------------------------------------------------
// 中转站未分组时 /v1/models 能返回 400+ 个模型，原生 select 只能靠滚动定位。这里把原生
// select 降级为「隐藏的值容器」（唯一值来源，样式见 styles.css #composerModelSelect），
// 在它之上叠一层可搜索浮层：输入即过滤、↑↓ 选择、Enter 确认、Esc 关闭。
// 选中与刷新一律回写 select.value + 派发 change，复用既有落库/重拉链路，不开第二条路径。

// 候选数据源 = select 的 options 本身，保证浮层与值容器**永不分叉**。
function composerPickerSource() {
  const select = $('#composerModelSelect');
  if (!select) return [];
  return [...select.options]
    // 空值占位项不是模型、「↻ 重新检测模型」是动作（各有独立入口），都不进候选列表。
    .filter((option) => option.value !== '' && option.value !== COMPOSER_MODEL_REFRESH)
    .map((option) => ({
      id: option.value,
      label: option.textContent.trim() || option.value,
      selected: option.selected,
    }));
}

function setComposerPickerActive(index) {
  const list = $('#composerModelList');
  const total = composerPickerState.items.length;
  if (!list || !total) {
    composerPickerState.activeIndex = -1;
    return;
  }
  // 环绕取模：到底继续按 ↓ 回到第一项（与 @ 文件弹层同一口径）。
  const next = ((index % total) + total) % total;
  composerPickerState.activeIndex = next;
  list.querySelectorAll('[data-model-index]').forEach((element) => {
    element.classList.toggle('is-active', Number(element.dataset.modelIndex) === next);
  });
  list.querySelector('.is-active')?.scrollIntoView({ block: 'nearest' });
}

// 按关键字重建候选列表（模型 id 与显示名任一命中即可）；query 为空 = 列出全部。
export function filterComposerModelPicker(query = '') {
  const list = $('#composerModelList');
  if (!list) return;
  const source = composerPickerSource();
  const needle = String(query || '').trim().toLowerCase();
  const items = needle
    ? source.filter((item) => item.id.toLowerCase().includes(needle)
      || item.label.toLowerCase().includes(needle))
    : source;
  composerPickerState.items = items;
  // 400+ 条一次性构建：与原生 select 本就要渲染同样数量的 option 同量级，
  // 用 fragment 只插一次，不做虚拟滚动（过滤是单次线性扫描，无需防抖）。
  const fragment = document.createDocumentFragment();
  items.forEach((item, index) => {
    const option = document.createElement('button');
    option.type = 'button';
    option.className = 'composer-model-option';
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', item.selected ? 'true' : 'false');
    option.dataset.modelIndex = String(index);
    option.title = item.id;
    option.textContent = item.label;
    fragment.append(option);
  });
  list.replaceChildren(fragment);
  list.hidden = items.length === 0;
  const empty = $('#composerModelEmpty');
  if (empty) empty.hidden = items.length > 0;
  const count = $('#composerModelCount');
  if (count) count.textContent = needle ? `${items.length}/${source.length}` : String(source.length);
  // 光标优先落在「当前已选模型」上：400+ 条时打开浮层就能看见自己在用的那个，
  // 不会一按 Enter 就把模型换成目录第一项（目录顺序由供应商决定，没有语义）。
  const selectedIndex = items.findIndex((item) => item.selected);
  setComposerPickerActive(selectedIndex >= 0 ? selectedIndex : 0);
}

// 把触发按钮与浮层对齐到 select 的当前状态。renderComposerModels 末尾、change 之后、
// 刷新结束时都会调用，所以「目录/会话/API 变了」与「程序直接改 select.value」两条路都不会留下过期显示。
export function syncComposerModelPicker() {
  const select = $('#composerModelSelect');
  const trigger = $('#composerModelTrigger');
  if (!select || !trigger) return;
  const value = String(select.value || '');
  const label = select.selectedOptions[0]?.textContent.trim() || '';
  // 刷新伪选项只是动作、不是模型：按钮文案保持上一个模型名，不闪成「↻ 重新检测模型」。
  if (value !== COMPOSER_MODEL_REFRESH) {
    const text = $('#composerModelTriggerText');
    if (text) text.textContent = label || '请先在设置中检查模型';
  }
  trigger.disabled = select.disabled;
  trigger.title = value && value !== COMPOSER_MODEL_REFRESH ? `当前模型：${label || value}（点击搜索切换）` : '选择模型';
  if (!composerPickerState.open) return;
  // 刷新进行中（select 被禁用）不能继续选：直接收起，避免选到过期项。
  if (select.disabled) {
    closeComposerModelPicker();
    return;
  }
  filterComposerModelPicker($('#composerModelSearch')?.value || '');
}

export function positionComposerModelPicker() {
  const panel = $('#composerModelPanel');
  const trigger = $('#composerModelTrigger');
  if (!panel || !trigger || panel.hidden) return;
  const rect = trigger.getBoundingClientRect();
  const gap = 8;
  const edge = 12;
  const above = rect.top - gap - edge;
  const below = window.innerHeight - rect.bottom - gap - edge;
  // 输入区在底部 → 默认朝上展开；朝上的空间不足 180px（列太扁没法挑）时改朝下。
  let openUp = above >= below;
  if ((openUp ? above : below) < 180) openUp = !openUp;
  // 高度上限 = 该侧可用空间，候选列自己滚。**不能只把 top 夹进视口**：400+ 条时面板
  // 比按钮上方还高，那样会把整块夹到视口边缘、离按钮几百像素（实测过的「弹层到处飘」）。
  panel.style.maxHeight = `${Math.round(Math.max(120, openUp ? above : below))}px`;
  const panelRect = panel.getBoundingClientRect();
  // 朝上时锚**底边**（bottom）而不是算 top：过滤后高度会从 426 缩到 134，
  // 锚底边高度变化就只往上长，永远贴着触发按钮；只算 top 会原地收缩、留一大截空白。
  if (openUp) {
    panel.style.bottom = `${Math.round(window.innerHeight - rect.top + gap)}px`;
    panel.style.top = 'auto';
  } else {
    panel.style.top = `${Math.round(Math.min(rect.bottom + gap, Math.max(edge, window.innerHeight - panelRect.height - edge)))}px`;
    panel.style.bottom = 'auto';
  }
  // 与审批模式菜单同一口径：左对齐触发按钮、夹在视口内（窄屏按钮靠边时不溢出）。
  const left = Math.min(
    Math.max(edge, rect.left),
    Math.max(edge, window.innerWidth - panelRect.width - edge),
  );
  panel.style.left = `${Math.round(left)}px`;
}

export function closeComposerModelPicker() {
  composerPickerState.open = false;
  composerPickerState.activeIndex = -1;
  composerPickerState.items = [];
  const panel = $('#composerModelPanel');
  if (panel) panel.hidden = true;
  $('#composerModelTrigger')?.setAttribute('aria-expanded', 'false');
}

export function openComposerModelPicker() {
  const panel = $('#composerModelPanel');
  const trigger = $('#composerModelTrigger');
  const select = $('#composerModelSelect');
  if (!panel || !trigger || !select || select.disabled) return;
  // 浮层挂到 body 并 fixed 定位：避免被 .composer-wrap 的 overflow 裁剪（教训 §九.27）；
  // 编辑历史消息时整个 composer 会被搬进 .message-row，也只有挂 body 才躲得开。
  if (panel.parentElement !== document.body) document.body.appendChild(panel);
  composerPickerState.open = true;
  panel.hidden = false;
  trigger.setAttribute('aria-expanded', 'true');
  const search = $('#composerModelSearch');
  if (search) search.value = '';
  // 先定位再填列表：定位锚的是底边、高度上限看的是可用空间，都与内容高度无关；
  // 反过来（先填列表）会让「滚到当前已选模型」在面板还没定位时执行。
  positionComposerModelPicker();
  filterComposerModelPicker('');
  search?.focus();
}

export function toggleComposerModelPicker() {
  if (composerPickerState.open) closeComposerModelPicker();
  else openComposerModelPicker();
}

// 关面板 → 回写 select → 派发 change：落库/强制重拉都仍走 saveComposerModelSelection 那一条链路。
function commitComposerModelPicker(id, { focusInput = true } = {}) {
  const select = $('#composerModelSelect');
  if (!select) return;
  closeComposerModelPicker();
  // 目录可能在面板打开期间变了：过期项直接丢弃，不写进 select。
  if (![...select.options].some((option) => option.value === id)) return;
  select.value = id;
  select.dispatchEvent(new Event('change'));
  if (focusInput) $('#messageInput')?.focus();
}

export function handleComposerModelPickerKey(event) {
  if (!composerPickerState.open) return;
  if (event.isComposing) return; // 输入法组词期间不抢键（与 @ 文件弹层同口径）
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (!composerPickerState.items.length) return;
    setComposerPickerActive(composerPickerState.activeIndex + (event.key === 'ArrowDown' ? 1 : -1));
    return;
  }
  if (event.key === 'Enter') {
    event.preventDefault();
    const item = composerPickerState.items[composerPickerState.activeIndex];
    if (item) commitComposerModelPicker(item.id);
    return;
  }
  if (event.key === 'Escape') {
    // stopPropagation：文档级另有审批菜单/会话菜单的 Esc 处理，不能被连带触发。
    event.preventDefault();
    event.stopPropagation();
    closeComposerModelPicker();
    $('#composerModelTrigger')?.focus();
  }
}

export function handleComposerModelPickerClick(event) {
  // 「↻ 重新检测模型」走原有伪选项链路（saveComposerModelSelection 里早退到强制重拉）。
  if (event.target.closest?.('#composerModelRefresh')) {
    commitComposerModelPicker(COMPOSER_MODEL_REFRESH, { focusInput: false });
    return;
  }
  const option = event.target.closest?.('[data-model-index]');
  if (!option) return;
  const item = composerPickerState.items[Number(option.dataset.modelIndex)];
  if (item) commitComposerModelPicker(item.id);
}

export function localProviderKind(provider) {
  if (!provider) return '';
  const requestFormat = String(provider.request_format || '').toLowerCase();
  return ['ollama', 'lm_studio'].includes(requestFormat) ? requestFormat : '';
}

export function updateUnloadModelButton() {
  const busy = Boolean(state.chatRunId || state.taskSubmitting);
  // 顶栏的「卸载模型」按钮已移除（本地模型卸载统一在「设置 → API 供应商」里操作）。
  const settingsButton = $('#unloadProviderModel');
  if (settingsButton) {
    const providerId = $('#providerId')?.value || '';
    const provider = (state.bootstrap?.providers || []).find((item) => item.id === providerId);
    const kind = localProviderKind(provider);
    settingsButton.hidden = !kind;
    settingsButton.disabled = busy;
    settingsButton.title = kind ? `卸载${kind === 'ollama' ? ' Ollama' : ' LM Studio'} 当前模型` : '';
  }
}

export async function unloadProviderModel(provider) {
  const kind = localProviderKind(provider);
  if (!provider || !kind) {
    toast('当前供应商不是支持卸载的本地模型');
    return;
  }
  if (state.chatRunId || state.taskSubmitting) {
    toast('请先等待当前对话结束');
    return;
  }
  if (!confirm(`卸载${kind === 'ollama' ? ' Ollama' : ' LM Studio'} 模型"${provider.model}"？`)) return;
  $('#unloadProviderModel').disabled = true;
  try {
    const result = await api('/api/models/unload', {
      method: 'POST',
      body: { model_key: provider.model_key },
    });
    toast(`${result.provider} 模型已卸载，显存和内存将被回收`);
  } catch (error) {
    toast(`卸载失败：${error.message}`);
  } finally {
    updateUnloadModelButton();
  }
}

export async function unloadConfiguredProviderModel() {
  const providerId = $('#providerId').value;
  const provider = (state.bootstrap.model_profiles || state.bootstrap.providers || []).find((item) => item.id === providerId);
  await unloadProviderModel(provider);
}

export async function saveModelSelection() {
  const value = $('#modelSelect').value;
  const result = await api('/api/settings', { method: 'POST', body: { model_key: value } });
  Object.assign(state.bootstrap.settings, result.settings);
  state.bootstrap.default_model_key = result.default_model_key || value;
  if (state.conversationId) {
    try {
      const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
        method: 'POST',
        body: { model_key: value, model_name: '' },
      });
      const index = state.conversations.findIndex((item) => item.id === state.conversationId);
      if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    } catch (error) {
      console.debug('[naiba] 保存对话模型失败:', error.message);
    }
  }
  updateUnloadModelButton();
  // 切换 API 后清空会话模型，等待新 API 的检测目录填充下拉。
  await populateComposerModels('');
  toast('API 已切换');
}

// 根据对话已保存的 API 与模型名恢复选择；未绑定或已删除时回退到全局默认。
export function applyConversationModel(conversation) {
  const select = $('#modelSelect');
  if (!select) return;
  const target = String(conversation?.model_key || '');
  if (target && [...select.options].some((o) => o.value === target)) {
    select.value = target;
    updateUnloadModelButton();
    void populateComposerModels(String(conversation?.model_name || ''));
    return;
  }
  const fallback = String(state.bootstrap.default_model_key || '');
  if (fallback && [...select.options].some((o) => o.value === fallback)) {
    select.value = fallback;
  } else if (select.options.length) {
    select.selectedIndex = 0;
  }
  updateUnloadModelButton();
  void populateComposerModels(String(conversation?.model_name || ''));
}

export function renderAgents() {
  const select = $('#agentSelect');
  if (!select) return;
  const agents = state.bootstrap?.agents || [];
  select.innerHTML = '';
  agents.forEach((agent) => {
    const option = document.createElement('option');
    option.value = agent.id;
    option.textContent = agent.name;
    select.append(option);
  });
  if (!select.options.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '暂无 Agent';
    select.append(option);
  }
  applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
}

// 会话 Agent：首轮前可下拉选择；首轮固化工具集后只读展示（切换会破坏缓存，需新开对话）。
export function applyConversationAgent(conversation) {
  const select = $('#agentSelect');
  if (!select) return;
  const agents = state.bootstrap?.agents || [];
  const agentId = String(conversation?.agent_id || '');
  let agent = agents.find((a) => String(a.id) === agentId);
  const locked = Boolean(conversation?.enabled_tool_ids && conversation.enabled_tool_ids.length);
  if (agent && [...select.options].some((o) => o.value === agent.id)) {
    select.value = agent.id;
  } else {
    const fallback = String(state.bootstrap?.default_agent_id || '');
    if ([...select.options].some((o) => o.value === fallback)) {
      select.value = fallback;
    } else if (select.options.length) {
      select.selectedIndex = 0;
    }
    agent = agents.find((a) => String(a.id) === fallback) || agents[0] || null;
  }
  select.disabled = locked;
  select.title = locked
    ? `当前会话已绑定 Agent「${agent?.name || ''}」并固化工具集，会话内不可切换。如需切换，请让 AI 总结当前对话，复制总结后新开对话。`
    : '选择该会话使用的 Agent（发送首条消息后固化，之后不可切换）。';
  renderSkills($('#skillSearch')?.value || '');
}

export async function saveAgentSelection() {
  const value = $('#agentSelect').value;
  if (!state.conversationId) {
    toast('请先打开或新建一个对话');
    return;
  }
  try {
    const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
      method: 'POST',
      body: { agent_id: value },
    });
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    renderSidebar();
    renderSkills($('#skillSearch')?.value || '');
    toast('Agent 已切换');
    appendPresetSkillsToComposer(value);
  } catch (error) {
    toast(`切换失败：${error.message}`);
    applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
  }
}
