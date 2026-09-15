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
  // 聊天背景图（只铺对话区）：服务端 settings.chat_background 是唯一事实来源，
  // 这里保存当前生效值（含编辑器调出的取景），供设置面板/编辑器回填与失效兜底使用。
  chatBackground: { image: '', opacity: 0.35, crop: null, position_x: 50, position_y: 50, zoom: 1 },
  // 内置背景图清单（/api/backgrounds，首次打开设置时拉一次并缓存）。
  chatBackgroundPresets: [],
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
  // 背景图先用本地缓存画出来（含上次的取景与图片比例，否则每次刷新都要等 bootstrap 才出现，
  // 而且几何未知时会先画 cover 再跳一下）；首屏可能还没有 token（未登录），此时 URL 取不到图
  // 也无妨——bootstrap 后 syncAppearanceFromBootstrap 会用服务端值重画一次。
  const stored = readStoredChatBackground();
  if (stored.aspect) setChatBackgroundImageAspect(stored.aspect);
  applyChatBackground(stored);
  refreshChatBackgroundGeometry();
  watchChatBackgroundGeometry();
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  media?.addEventListener?.('change', () => {
    if (state.appearance?.theme === 'system') applyAppearance(state.appearance);
  });
  return initial;
}

// 对话区尺寸变化的跟踪：侧栏/文件面板开合不会触发 window.resize，所以额外挂一个
// ResizeObserver。编辑器打开期间**不刷新**（见 refreshChatBackgroundGeometry 的说明）。
let chatBackgroundGeometryWatched = false;

function watchChatBackgroundGeometry() {
  if (chatBackgroundGeometryWatched) return;
  chatBackgroundGeometryWatched = true;
  const editorOpen = () => Boolean(document.getElementById('chatBackgroundDialog')?.open);
  const refresh = () => { if (!editorOpen()) refreshChatBackgroundGeometry(); };
  window.addEventListener('resize', refresh);
  const target = $('.chat-backdrop');
  if (target && typeof ResizeObserver === 'function') {
    new ResizeObserver(refresh).observe(target);
  }
}

// 在 bootstrap 完成后调用，服务端配置优先；旧版本无 appearance 时保留本地偏好。
export function syncAppearanceFromBootstrap(bootstrap) {
  const configured = bootstrap?.settings?.appearance || bootstrap?.appearance;
  const local = readStoredAppearance();
  const next = applyAppearance({
    theme: configured?.theme ?? local.theme,
    skin: configured?.skin ?? local.skin,
  });
  // 背景图与外观同一时机同步（同一个 settings 载荷，不必再等第二处调用）。
  // 内部自己做失效兜底，是 fire-and-forget，不阻塞首屏。
  void syncChatBackgroundFromBootstrap(bootstrap);
  return next;
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

// ---- 聊天背景图（只铺对话区） ----
// 与外观同一套策略：服务端 settings.chat_background 是唯一事实来源（同实例多端共享），
// localStorage 只用来"首屏先画出来"，随后由 bootstrap 的服务端值校正。
const CHAT_BACKGROUND_KEY = 'naibaChatBackground';
const CHAT_BACKGROUND_MIN_OPACITY = 0.05;
const CHAT_BACKGROUND_DEFAULT_OPACITY = 0.35;
// 能当背景的格式（与后端 config.CHAT_BACKGROUND_IMAGE_FORMATS 同口径）：必须在
// WebView2/Chromium 里能解码，否则就是"选图成功、背景一片空白"的静默失败
// （TIFF / HEIC 是实测踩过的坑）。这里只做选择前的前置过滤，**判定以后端为准**——
// 后端按图片内容识别，改名骗不过去。
export const CHAT_BACKGROUND_FORMATS = new Set(['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'avif']);
// 取景 = 自由裁剪区域 crop（图片内的相对矩形，见下方 chatBackgroundCrop）。
// position_* / zoom 是**旧模型的遗留输入**：旧模型把取景框比例锁死成对话区比例，只有
// "缩放 + 位置"两个自由度，于是「填满」「完整显示」两个预置态必然有一个方向自由度恰好
// 为 0（用户报障："取景框只能横向切割，不能竖向切割"）。crop 缺失时才用它们换算等价区域。
const CHAT_BACKGROUND_DEFAULT_POSITION = 50;
const CHAT_BACKGROUND_DEFAULT_ZOOM = 1;
// 与后端 CHAT_BACKGROUND_MIN/MAX_ZOOM 同口径（后端是权威，这里只做前端收敛）。
const CHAT_BACKGROUND_MIN_ZOOM = 0.05;
const CHAT_BACKGROUND_MAX_ZOOM = 4;
// 与后端 CHAT_BACKGROUND_MIN_CROP 同口径：取景区域的最小边长（图片比例）。
export const CHAT_BACKGROUND_MIN_CROP = 0.02;

// /api/file 的 URL 形状唯一定义点（03-media.js 的 fileUrl 复用它）。
// 放在 01-core 而不是 03-media：01-core 是底层模块，反向 import 03-media 会形成循环依赖。
export function localFileUrl(source) {
  return `/api/file?token=${encodeURIComponent(state.token)}&path=${encodeURIComponent(String(source || ''))}`;
}

// 几何量缓存：取景要按「图片原始比例 q」与「对话区盒子比例 r」算百分比尺寸。
// 两个值都不便宜（q 要探针、r 要量布局），所以只在打开/尺寸变化时算一次，拖拽时复用
// ——applyChatBackground 每次拖拽都会调用，绝不能在里面 measure（会逐帧强制布局）。
const chatBackgroundGeometry = { imageAspect: 0, boxAspect: 0 };

function readStoredChatBackground() {
  try {
    const raw = JSON.parse(localStorage.getItem(CHAT_BACKGROUND_KEY) || '{}');
    return {
      image: typeof raw.image === 'string' ? raw.image : '',
      opacity: clampChatBackgroundOpacity(raw.opacity),
      crop: normalizeChatBackgroundCrop(raw.crop),
      position_x: clampChatBackgroundPosition(raw.position_x),
      position_y: clampChatBackgroundPosition(raw.position_y),
      zoom: clampChatBackgroundZoom(raw.zoom),
      // 上次探到的图片比例：首屏要靠它算取景，否则要先渲染一帧 cover 再跳（闪跳）。
      aspect: Number(raw.aspect) > 0 ? Number(raw.aspect) : 0,
    };
  } catch (_) {
    return { image: '', opacity: CHAT_BACKGROUND_DEFAULT_OPACITY, aspect: 0 };
  }
}

export function clampChatBackgroundOpacity(value) {
  const opacity = Number(value);
  if (!Number.isFinite(opacity)) return CHAT_BACKGROUND_DEFAULT_OPACITY;
  return Math.min(1, Math.max(CHAT_BACKGROUND_MIN_OPACITY, opacity));
}

export function clampChatBackgroundPosition(value) {
  const position = Number(value);
  if (!Number.isFinite(position)) return CHAT_BACKGROUND_DEFAULT_POSITION;
  return Math.min(100, Math.max(0, position));
}

export function clampChatBackgroundZoom(value) {
  const zoom = Number(value);
  if (!Number.isFinite(zoom)) return CHAT_BACKGROUND_DEFAULT_ZOOM;
  return Math.min(CHAT_BACKGROUND_MAX_ZOOM, Math.max(CHAT_BACKGROUND_MIN_ZOOM, zoom));
}

// 图片原始比例 q（宽/高）：探针拿到后写入；0 = 未知（此时回落 cover）。
export function setChatBackgroundImageAspect(aspect) {
  const value = Number(aspect);
  chatBackgroundGeometry.imageAspect = Number.isFinite(value) && value > 0 ? value : 0;
  return chatBackgroundGeometry.imageAspect;
}

export function chatBackgroundImageAspect() {
  return chatBackgroundGeometry.imageAspect;
}

// 对话区盒子比例 r：量一次缓存起来。窗口/侧栏尺寸变化时由 refreshChatBackgroundGeometry
// 刷新；**编辑器打开期间不刷新**（白板比例与滑杆上下限按打开时量到的 r 定格，避免调图中
// 窗口一动取景就跟着跳——关掉重开即恢复精确）。
export function refreshChatBackgroundGeometry() {
  const element = $('.chat-backdrop') || $('#messages');
  const rect = element?.getBoundingClientRect?.();
  const aspect = rect && rect.height > 0 ? rect.width / rect.height : 0;
  const changed = Math.abs(aspect - chatBackgroundGeometry.boxAspect) > 0.001;
  chatBackgroundGeometry.boxAspect = aspect;
  if (changed) applyChatBackground({});
  return aspect;
}

export function chatBackgroundBoxAspect() {
  return chatBackgroundGeometry.boxAspect;
}

// 「填满」区域：图片内最大的、比例等于对话区比例（r）的居中矩形 = 旧模型 zoom=1 的观感。
// 取景框可自由改形状后，"填满"从"唯一的框形状"降级成一键预置，语义没变。
export function chatBackgroundFillCrop() {
  const q = chatBackgroundGeometry.imageAspect;
  const r = chatBackgroundGeometry.boxAspect;
  if (!(q > 0) || !(r > 0)) return { x: 0, y: 0, w: 1, h: 1 };
  const width = Math.min(1, r / q);
  const height = Math.min(1, q / r);
  return { x: (1 - width) / 2, y: (1 - height) / 2, w: width, h: height };
}

// 取景区域收敛：边长至少 CHAT_BACKGROUND_MIN_CROP，整体不许越出图片（越界按图片边界推回）。
// 编辑器每次拖拽/缩放都过它 → "框跑到图片外"这种状态在数据层就不可能存在（也就不会出现
// 用户拖了半天却什么都没发生：现在的框永远在图片里，两个方向都永远有可动空间）。
export function clampChatBackgroundCrop(crop) {
  const raw = crop && typeof crop === 'object' ? crop : {};
  const width = Math.min(1, Math.max(CHAT_BACKGROUND_MIN_CROP, Number(raw.w) || 0));
  const height = Math.min(1, Math.max(CHAT_BACKGROUND_MIN_CROP, Number(raw.h) || 0));
  const x = Math.min(1 - width, Math.max(0, Number(raw.x) || 0));
  const y = Math.min(1 - height, Math.max(0, Number(raw.y) || 0));
  return { x, y, w: width, h: height };
}

// 本地归一化（与后端 normalize_chat_background_crop 同口径）：结构不可用 → null（= 自动）。
function normalizeChatBackgroundCrop(value) {
  if (!value || typeof value !== 'object') return null;
  const numbers = {};
  for (const key of ['x', 'y', 'w', 'h']) {
    const number = Number(value[key]);
    if (!Number.isFinite(number)) return null;
    numbers[key] = number;
  }
  // 宽高非正 = "没有取景" → 回落自动（与后端同口径，别把它收敛成一条缝）。
  if (!(numbers.w > 0) || !(numbers.h > 0)) return null;
  return clampChatBackgroundCrop(numbers);
}

// 当前生效的取景区域：crop 优先；缺失时按旧模型（zoom + 位置）换算等价区域，再收敛进图片。
// 换算对"填满 / 完整显示"这两个预置态是**逐像素等价**的（旧模型的框比例恒等于对话区比例，
// 新模型算出来是同一个矩形），只有"缩到比对话区还小、四周全靠留白"的特例会被收敛成
// "整张图等比铺"——后者的观感更好，且留白照旧由模糊底补上。
export function chatBackgroundCrop(background = state.chatBackground || {}) {
  const explicit = background?.crop;
  if (explicit && Number(explicit.w) > 0 && Number(explicit.h) > 0) {
    return clampChatBackgroundCrop(explicit);
  }
  const zoom = clampChatBackgroundZoom(background?.zoom);
  if (Math.abs(zoom - 1) < 1e-6) return chatBackgroundFillCrop();
  const q = chatBackgroundGeometry.imageAspect;
  const r = chatBackgroundGeometry.boxAspect;
  if (!(q > 0) || !(r > 0)) return { x: 0, y: 0, w: 1, h: 1 };
  // 旧模型：可见区宽 = 1/zoom（图片单位）、高 = 宽 × q/r；左上角 = (1 - 宽) × 位置% / 100。
  const width = 1 / zoom;
  const height = width * q / r;
  return clampChatBackgroundCrop({
    x: (1 - width) * clampChatBackgroundPosition(background?.position_x) / 100,
    y: (1 - height) * clampChatBackgroundPosition(background?.position_y) / 100,
    w: width,
    h: height,
  });
}

// 「缩放」读数：相对「填满」的线性倍率（= sqrt(填满面积 / 取景面积)）。
// 对比例等于对话区的取景，它与旧模型的 zoom 数值**完全一致**（迁移不改读数）；
// 自由形状的取景则是一个同样单调的近似读数（滑杆/滚轮只做等比缩放，不会改形状）。
export function chatBackgroundCropScale(crop = chatBackgroundCrop()) {
  const fill = chatBackgroundFillCrop();
  const area = Math.max(1e-9, Math.abs(crop.w * crop.h));
  return Math.sqrt((fill.w * fill.h) / area);
}

// 缩放滑杆范围：下限 = 整张图（取景最大），上限 = 最小取景（封顶 CHAT_BACKGROUND_MAX_ZOOM）。
export function chatBackgroundCropScaleLimits() {
  const fill = chatBackgroundFillCrop();
  const fillArea = Math.max(1e-9, fill.w * fill.h);
  return {
    min: Math.min(1, Math.sqrt(fillArea)),
    max: Math.min(CHAT_BACKGROUND_MAX_ZOOM, Math.sqrt(fillArea) / CHAT_BACKGROUND_MIN_CROP),
  };
}

// 把取景区域映射成 CSS：图片按"取景块等比铺满对话区（contain-fit，不拉伸）"来画，
// 再解出让取景块居中的 background-position 百分比。留白（未被取景层覆盖的地方）由
// .chat-bg-blur 的模糊底补上——所以这里只负责"铺得对不对"，不负责盖满。
// 百分比相对元素自身盒子解析，因此真实对话区（大盒子）与白板/缩略图（小盒子、同比例）
// 渲染出的取景完全一致：编辑器里调的就是关掉后看到的。
// 未知 q/r 时返回 null → CSS 回落 `cover`/居中（无几何信息时的默认观感）。
function chatBackgroundFit(crop) {
  const q = chatBackgroundGeometry.imageAspect;
  const r = chatBackgroundGeometry.boxAspect;
  if (!(q > 0) || !(r > 0)) return null;
  const regionAspect = q * crop.w / crop.h;
  let sizeX;   // 图片的绘制宽度（相对对话区宽度的百分比）
  let sizeY;   // 图片的绘制高度（相对对话区高度的百分比）
  if (regionAspect >= r) {
    // 取景块比对话区"更宽" → 按宽度顶满（高度自然不满，露出模糊底）。
    sizeX = 100 / crop.w;
    sizeY = sizeX * r / q;
  } else {
    sizeY = 100 / crop.h;
    sizeX = sizeY * q / r;
  }
  const denominatorX = 1 - sizeX / 100;
  const denominatorY = 1 - sizeY / 100;
  const positionX = Math.abs(denominatorX) > 1e-6
    ? 100 * (0.5 - (crop.x + crop.w / 2) * sizeX / 100) / denominatorX : 50;
  const positionY = Math.abs(denominatorY) > 1e-6
    ? 100 * (0.5 - (crop.y + crop.h / 2) * sizeY / 100) / denominatorY : 50;
  return {
    size: `${sizeX.toFixed(2)}% ${sizeY.toFixed(2)}%`,
    position: `${positionX.toFixed(2)}% ${positionY.toFixed(2)}%`,
    // 取景块形状正好等于对话区形状 → 铺满，不需要模糊底。
    covered: Math.abs(regionAspect - r) < 0.002,
  };
}

// 背景层 / 白板 / 缩略图的**唯一写入点**：写 :root 上的 CSS 变量，三处共用同一组变量，
// 所以强度、位置、缩放不可能出现"某个视图没跟上"。
export function applyChatBackground(background = {}) {
  const previous = state.chatBackground || {};
  const next = {
    image: String(background.image ?? previous.image ?? ''),
    opacity: clampChatBackgroundOpacity(background.opacity ?? previous.opacity),
    // crop 的显式 null = 恢复"自动"（按对话区比例取最大区域），所以必须看键在不在，
    // 用 ?? 会把 null 当"没给"吞掉，结果就是"清不掉取景"。
    crop: 'crop' in background ? normalizeChatBackgroundCrop(background.crop) : (previous.crop ?? null),
    // 旧字段只维护不写回：crop 缺失时它们是换算输入（旧客户端/旧配置的升级路径）。
    position_x: clampChatBackgroundPosition(background.position_x ?? previous.position_x),
    position_y: clampChatBackgroundPosition(background.position_y ?? previous.position_y),
    zoom: clampChatBackgroundZoom(background.zoom ?? previous.zoom),
  };
  state.chatBackground = next;
  const root = document.documentElement;
  root.style.setProperty('--chat-bg-opacity', String(next.opacity));
  const fit = next.image ? chatBackgroundFit(chatBackgroundCrop(next)) : null;
  // 取景块形状 ≠ 对话区形状时铺不满，留白交给同图的模糊底（"完整显示"等状态本来就这样）。
  root.dataset.chatBgCover = !next.image || !fit || fit.covered ? '1' : '0';
  if (next.image) {
    root.style.setProperty('--chat-bg-image', `url("${localFileUrl(next.image)}")`);
    if (fit) {
      root.style.setProperty('--chat-bg-size', fit.size);
      root.style.setProperty('--chat-bg-position', fit.position);
    } else {
      // 几何未知：让 CSS 用默认的 cover/居中（无取景信息时的默认观感），别拿旧尺寸硬套新图。
      root.style.removeProperty('--chat-bg-size');
      root.style.removeProperty('--chat-bg-position');
    }
    if (chatBackgroundGeometry.boxAspect > 0) {
      root.style.setProperty('--chat-bg-ratio', String(chatBackgroundGeometry.boxAspect));
    }
  } else {
    root.style.removeProperty('--chat-bg-image');
    root.style.removeProperty('--chat-bg-size');
    root.style.removeProperty('--chat-bg-position');
  }
  const preview = $('#chatBackgroundPreview');
  if (preview) preview.hidden = !next.image;
  try {
    // aspect 一起缓存：首屏预渲染要用它算取景，否则会"先 cover 再跳一下"。
    localStorage.setItem(CHAT_BACKGROUND_KEY, JSON.stringify({
      ...next,
      aspect: chatBackgroundGeometry.imageAspect || undefined,
    }));
  } catch (_) { /* storage disabled */ }
  return next;
}

// bootstrap 完成后调用：服务端值优先，并顺手处理"图片已不存在"的死链设置。
export async function syncChatBackgroundFromBootstrap(bootstrap) {
  const configured = bootstrap?.settings?.chat_background;
  const local = readStoredChatBackground();
  // 先用本地缓存（含上次探到的图片比例）画一帧：首屏不会因为几何未知而闪一下。
  if (!chatBackgroundImageAspect() && local.aspect) setChatBackgroundImageAspect(local.aspect);
  applyChatBackground({
    image: configured?.image ?? local.image,
    opacity: configured?.opacity ?? local.opacity,
    // 服务端给了 chat_background 就以它的 crop 为准（含显式 null = 自动）；老服务端没有该
    // 字段时用本地缓存，再靠下面的 position/zoom（旧模型字段）兜底换算。
    crop: configured ? normalizeChatBackgroundCrop(configured.crop) : (local.crop ?? null),
    position_x: configured?.position_x ?? local.position_x,
    position_y: configured?.position_y ?? local.position_y,
    zoom: configured?.zoom ?? local.zoom,
  });
  const configuredImage = String(configured?.image || '');
  if (!configuredImage) return state.chatBackground;
  refreshChatBackgroundGeometry();
  // 图片可能已被缓存清理 / 换机器后 data_dir 迁移失效：探一次，坏链就地清空并提示，
  // 不留一条"看着有设置、其实什么都没有"的死链。探针同时把图片原始比例 q 带回来，
  // 取景（位置/缩放）要靠 q 才算得出来。
  const probe = await probeChatBackgroundImage(localFileUrl(configuredImage));
  if (!probe) {
    await clearChatBackgroundSetting();
    toast('背景图文件已失效，已清除背景设置');
    return state.chatBackground;
  }
  const aspect = probe.naturalWidth / probe.naturalHeight;
  if (Number.isFinite(aspect) && aspect > 0 && Math.abs(aspect - chatBackgroundImageAspect()) > 0.0001) {
    setChatBackgroundImageAspect(aspect);
    // q 到位后重画一次：首次（本地缓存没有 aspect 时）这一笔会把取景补正到精确值。
    applyChatBackground({});
  }
  return state.chatBackground;
}

// 探针：成功返回已解码的 Image（naturalWidth/Height 可用于算比例），失败返回 null。
function probeChatBackgroundImage(url) {
  return new Promise((resolve) => {
    const probe = new Image();
    probe.onload = () => resolve(probe);
    probe.onerror = () => resolve(null);
    probe.src = url;
  });
}

// 编辑器/设置面板打开前也可以主动探一次（换图后要立刻拿到新 q）。
export async function probeChatBackgroundImageAspect(path) {
  const probe = await probeChatBackgroundImage(localFileUrl(path));
  if (!probe) return 0;
  const aspect = probe.naturalWidth / probe.naturalHeight;
  setChatBackgroundImageAspect(aspect);
  return chatBackgroundImageAspect();
}

// 保存背景设置（唯一写入通道）。失败时抛出，由调用方决定怎么提示。
export async function saveChatBackground(patch = {}) {
  const next = applyChatBackground({ ...state.chatBackground, ...patch });
  const result = await api('/api/settings', { method: 'POST', body: { chat_background: next } });
  const saved = result?.settings?.chat_background || result?.chat_background;
  if (saved) applyChatBackground(saved);
  if (state.bootstrap?.settings) state.bootstrap.settings.chat_background = saved || next;
  return state.chatBackground;
}

// 「清除背景」与"图片失效兜底"共用：先清设置（清完才不再算"在用"），再尽力回收旧文件。
export async function clearChatBackgroundSetting({ reclaim = '' } = {}) {
  const previous = String(state.chatBackground?.image || '');
  applyChatBackground({ image: '' });
  try {
    await saveChatBackground({ image: '' });
  } catch (_) {
    // 服务端不可达时本地已清空，下次启动同步会再试一次；不把这里变成未捕获的拒绝。
  }
  const target = String(reclaim || previous || '');
  if (target) {
    // 回收失败不影响"已清除"（文件有引用或已被清理时服务端会拒绝）。
    api('/api/uploads/delete', { method: 'POST', body: { path: target } }).catch(() => { /* 交给清理机制 */ });
  }
  return state.chatBackground;
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
