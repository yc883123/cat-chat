// ============================================================
// 15-bind-events.js —— 拆分自 public/app.js 第 6658-7653 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, $$, api, applyAppearance, applyChatBackground, CHAT_BACKGROUND_FORMATS, CHAT_BACKGROUND_MIN_CROP, chatBackgroundCrop, chatBackgroundCropScale, chatBackgroundCropScaleLimits, chatBackgroundFillCrop, clampChatBackgroundCrop, clampChatBackgroundOpacity, clearChatBackgroundSetting, contextMenuPreviousFocus, copyText, draggedFileCache, editableElement, ensureContextMenu, hideTextContextMenu, onChatBackgroundMissingChange, probeChatBackgroundImageAspect, refreshChatBackgroundGeometry, restoreTopbarCompact, runTextContextAction, saveAppearance, saveChatBackground, setTopbarCompact, showTextContextMenu, state, toast, topLayerContainer } from "./01-core.js";
import { closeContextUsagePopover, closeImageLightbox, continueAfterContextWarning, ensureImageContextMenu, handleImageLightboxKey, hideImageContextMenu, initImageLightboxInteractions, isPywebview, openImageLightbox, positionContextUsagePopover, resetContextWarningResume, runImageContextAction, showImageContextMenu, stepImageLightbox, toggleContextUsagePopover, updateSendButtonState } from "./03-media.js";
import { branchMessage, cancelActiveEdit, cancelSessionStart, confirmActiveEdit, deleteMessageFlow, fillContextResetSeed, initTurnRail, isNearBottom, regenerateMessage, setStickToBottom, startEditMessage, startNewSession, undoLastDelete } from "./04-messages.js";
import { authenticate, enableLanAccess, initialize } from "./05-bootstrap.js";
import { switchPermissionMode } from "./06-tasks-plans.js";
import { checkUpdate, closeAgentHelpPopover, closeComposerModelPicker, composerPickerState, filterComposerModelPicker, handleComposerModelPickerClick, handleComposerModelPickerKey, installUpdate, positionAgentHelpPopover, positionComposerModelPicker, renderUpdateStatus, saveAgentSelection, saveComposerModelSelection, saveModelSelection, syncComposerModelPicker, toggleAgentHelpPopover, toggleComposerModelPicker, unloadConfiguredProviderModel, unloadProviderModel } from "./07-models-agents.js";
import { cancelTask, clearTerminalTasks, closeAgentPromptPresetPanel, closeBranchChainPanel, closeConversationMenu, conversationMenuTargetId, createWorkspace, deleteConversation, handleAgentPromptPresetPanelClick, importAgentCharacterCard, onComposerWorkspaceChange, onSidebarTreeClick, openAgentPromptPresetSaveDialog, openConversation, openRenameConversation, positionAgentPromptPresetPanel, renderSidebar, renderSidebarWindow, runFullTextSearch, saveAgentPromptPreset, saveNewWorkspace, saveRenameConversation, setSidebarScrollRaf, sidebarRowCache, sidebarScrollRaf, toggleAgentPromptPresetPanel, setTaskLogOpen, setTaskLogStick, setWorkspaceSearchMode, syncSearchModeUi, SEARCH_DEBOUNCE_MS } from "./08-conversations.js";
import { addProvider, addSearchProfile, appearanceFormValues, applyProviderModelCapabilities, applyProviderPreset, cancelProviderEdit, cleanImageCache, closeAgentToolEditor, compactDatabase, deleteAgent, deleteProvider, deleteSearchProfile, deleteVisionProvider, hideAgentForm, handleAgentAvatarFile, handleAgentToolPresetCardsClick, handleAgentToolPresetCardsKeydown, loadMcpServers, loadProviderModels, loadStorageStats, loadWorkspaceTree, openAgentCard, openProviderCard, openProviderPresetKeyUrl, openVisionProviderForm, persistSearchProfiles, loadChatBackgroundPresets, pickAgentAvatar, pickWorkspace, populateChatBackgroundEditor, refreshImageCacheSize, renderAgentManager, renderAgentSkillPicker, renderImageCompressRow, renderProviders, renderProxyRows, renderSearchProfileFields, renderSkills, renderToolScopeList, saveAccessToken, saveAgentForm, saveAgentToolSet, saveMcpServer, saveProvider, saveRuntimeSettings, saveSearchSettings, saveVisionSettings, saveWorkspaceSettings, searchProfiles, setChatBackgroundEditorEnabled, setChatBackgroundEditorError, setChatBackgroundStatus, showAgentForm, switchAgentTab, syncAppearanceControls, syncProviderKindOptions, testProvider, testSearchConnection, testVisionConnection, toggleAllToolGroups, toggleCustomModel, toggleProviderKey, updateAgentSkillTabCount, updateChatBackgroundControls, updateChatBackgroundEditorControls, updateProviderContextField, updateProviderFormatGuide, updateProviderVisionHint } from "./09-settings.js";
import { addFolderChip, isFolderChip, readAsDataUrl, renderPendingFiles, uploadFiles } from "./10-upload.js";
import { cancelCurrentRun, closeQuickMessagePanel, closeReasoningMenu, handleQuickMessagePanelClick, handlePasteImage, openStarterPromptDialog, positionQuickMessagePanel, positionReasoningMenu, quickPanelState, reloadPage, restoreStarterPresets, saveStarterPrompt, sendMessage, setReasoningEffort, startSkillEdit, startSkillInstall, toggleDeepReasoning, toggleQuickMessagePanel, togglePermissionModeMenu, positionPermissionModeMenu, closePermissionModeMenu, permissionMenuState } from "./12-chat-input.js";
import { commitSkillSelection, hideSkillPopup, insertSkillRefAtCursor, moveSkillPopupSelection, popupState, positionSkillPopup, renderInputMirror, resizeTextarea, setSkillPopupSelection, skillList, updateSkillPopup } from "./13-skill-refs.js";
import { activateFileTab, activeFileTab, applyFilePanelOpenClass, cancelFileEdit, closeFilePanel, closeSidebar, filePanelState, filePanelUsable, openFilePanel, openSidebar, removeFileTab, reopenFilePanel, restoreLeftSidebarCollapse, saveFileTab, setLeftSidebarCollapsed, sidebarDesktop, startFileEdit, updateFileTabsButton } from "./14-file-panel.js";
import { handleFilePopupClick, handleFilePopupKey, positionFilePopup, updateFilePopup } from "./16-file-refs.js";
import { guideAllInterjections, sendRunInterjection } from "./18-interjections.js";
import { dismissOnboarding, resetOnboarding, saveOnboardingProvider, selectOnboardingPreset, testOnboardingConnection } from "./19-onboarding.js";
// ---- 聊天背景图（外观页） ----
// 上传走同一个 /api/uploads（落盘 data/uploads/<日期>/，返回绝对路径），但**不进入
// state.pendingFiles**——那是"随消息发送的附件"，背景图不该出现在输入框上方。
function uploadChatBackgroundFile(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/uploads');
    if (state.token) xhr.setRequestHeader('Authorization', `Bearer ${state.token}`);
    xhr.onload = () => {
      let payload = {};
      try { payload = JSON.parse(xhr.responseText || '{}'); } catch (_) { /* 非 JSON 错误体 */ }
      if (xhr.status >= 200 && xhr.status < 300 && payload.path) resolve(payload);
      else reject(new Error(payload.error || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('网络错误'));
    const form = new FormData();
    form.append('file', file, file.name);
    xhr.send(form);
  });
}

function resetChatBackgroundFileInput() {
  const input = $('#chatBackgroundFile');
  if (input) input.value = '';
}

// 选图 → 上传 → 保存设置。前置只做轻量过滤（扩展名），**格式判定以后端为准**：
// 后端按图片内容识别，TIFF/HEIC 改名成 .png 也照样被拦下。
async function handleChatBackgroundFile(file) {
  if (!file) return;
  const name = String(file.name || '');
  const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
  if (!CHAT_BACKGROUND_FORMATS.has(ext)) {
    setChatBackgroundStatus('背景图仅支持 PNG / JPG / WebP / GIF / BMP / AVIF', true);
    resetChatBackgroundFileInput();
    return;
  }
  // 整份快照（不只是 image）：失败回滚要连取景一起还回去——换图时取景被复位成居中/填满，
  // 只回滚图片会留下"图回去了、位置却变了"的半截状态。
  const snapshot = { ...(state.chatBackground || {}) };
  const previous = String(snapshot.image || '');
  setChatBackgroundStatus(`正在上传「${name}」…`);
  let uploaded;
  try {
    uploaded = await uploadChatBackgroundFile(file);
  } catch (error) {
    setChatBackgroundStatus(`上传失败：${error.message}`, true);
    resetChatBackgroundFileInput();
    return;
  }
  try {
    // 先探新图比例：q 到手后保存那一帧就是正确取景（否则会先按上一张图的几何画一下）。
    await probeChatBackgroundImageAspect(uploaded.path);
    // 换图 = 取景复位（crop: null = 自动按对话区比例取最大区域居中）：新图的构图与上一张
    // 无关，沿用旧取景只会莫名其妙。旧字段一起归零，免得 crop 缺失时又按旧值算出偏移。
    await saveChatBackground({ image: uploaded.path, crop: null, position_x: 50, position_y: 50, zoom: 1 });
  } catch (error) {
    // saveChatBackground 是"乐观应用 + 落库"：服务端拒绝时必须把图层回滚到上一张，
    // 否则界面留着"设置失败、背景却已变空白"的假象（被拒的图浏览器多半也解不出来）。
    applyChatBackground(snapshot);
    updateChatBackgroundControls();
    setChatBackgroundStatus(`保存失败：${error.message}`, true);
    // 存不进设置的上传文件立即回收，别在缓存目录里留垃圾；但与当前背景同路径时不能删。
    if (uploaded.path !== previous) {
      api('/api/uploads/delete', { method: 'POST', body: { path: uploaded.path } }).catch(() => { /* 交给清理机制 */ });
    }
    resetChatBackgroundFileInput();
    return;
  }
  // 换图成功后再回收上一张：新图已写进设置，旧图才不再算"在用"（否则会被判为引用中拒绝删除）。
  // 上一张是内置背景时跳过（它在 data/backgrounds，删不掉也不该删）。
  if (previous && previous !== uploaded.path && !isBuiltInBackground(previous)) {
    reclaimBackgroundFile(previous);
  }
  updateChatBackgroundControls();
  setChatBackgroundStatus('背景图已更新');
  resetChatBackgroundFileInput();
}

// 点内置背景：与"选图"走**同一条**保存通道（先探比例 → 取景复位 → 落库），只是来源是内置目录。
// 内置图在 data/backgrounds 下、不在 uploads 里，所以不参与缓存回收（清除背景时的回收请求
// 会被服务端按"非 uploads 路径"拒绝，属预期，不影响清除本身）。
async function applyChatBackgroundPreset(path) {
  const target = String(path || '');
  if (!target || target === String(state.chatBackground?.image || '')) return;
  const row = $('#chatBackgroundPresetsRow');
  const buttons = row ? [...row.querySelectorAll('button')] : [];
  buttons.forEach((button) => { button.disabled = true; });
  setChatBackgroundStatus('正在应用内置背景…');
  const snapshot = { ...(state.chatBackground || {}) };
  try {
    await probeChatBackgroundImageAspect(target);
    await saveChatBackground({ image: target, crop: null, position_x: 50, position_y: 50, zoom: 1 });
    setChatBackgroundStatus('已应用内置背景，可用「调整背景图」继续微调');
  } catch (error) {
    // 乐观应用失败要回滚（与选图同一条铁律），否则界面上留着"应用失败但背景已变"的假象。
    applyChatBackground(snapshot);
    setChatBackgroundStatus(`应用失败：${error.message}`, true);
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
    updateChatBackgroundControls();
  }
}

function bindChatBackgroundControls() {
  const pick = $('#pickChatBackground');
  const file = $('#chatBackgroundFile');
  const clear = $('#clearChatBackground');
  const slider = $('#chatBackgroundOpacity');
  // 启动探针（bootstrap 同步）是异步的：结果回来后重画设置卡——「背景图文件暂不可用」
  // 提示的唯一渲染点是 updateChatBackgroundControls（01-core 不能反向 import 设置面板）。
  onChatBackgroundMissingChange(() => updateChatBackgroundControls());
  if (!pick || !file) return;
  pick.addEventListener('click', () => file.click());
  file.addEventListener('change', () => { void handleChatBackgroundFile(file.files?.[0]); });
  // 内置背景用事件委托：那一排是 renderChatBackgroundPresets() 整排重绘的。
  $('#chatBackgroundPresetsRow')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-chat-bg-preset]');
    if (button) void applyChatBackgroundPreset(button.dataset.chatBgPreset);
  });
  clear?.addEventListener('click', async () => {
    if (!state.chatBackground?.image) return;
    clear.disabled = true;
    setChatBackgroundStatus('正在清除…');
    await clearChatBackgroundSetting();
    updateChatBackgroundControls();
    setChatBackgroundStatus('背景已清除');
  });
  if (!slider) return;
  // 拖动时本地实时预览（不落库），松手（change）才保存——滑杆不该每动一格发一次请求。
  slider.addEventListener('input', () => {
    applyChatBackground({ opacity: clampChatBackgroundOpacity(slider.value) });
    updateChatBackgroundControls();
  });
  slider.addEventListener('change', async () => {
    try {
      await saveChatBackground({ opacity: clampChatBackgroundOpacity(slider.value) });
      updateChatBackgroundControls();
      setChatBackgroundStatus('背景强度已保存');
    } catch (error) {
      setChatBackgroundStatus(`保存失败：${error.message}`, true);
    }
  });
}

// ---- 背景图编辑器（概览 + 取景框；交互数学与 CSS 那套百分比同源） ----
// 交互期间**只改本地显示**（applyChatBackground 写 CSS 变量），不发任何请求；
// 点「完成」才落库。原因：落库与"取消回滚"不能并存——中途落一次库，用户按取消
// 就只回滚了本地，下次刷新又变回中途那个值（前后端不一致）。
// 唯一例外是「旋转 90°」：它要在服务端把图真的转成新文件，属于"换图"而不是"取景"，
// 所以由它自己负责回滚与派生文件回收（见 rotateChatBackgroundImage）。
// 关闭路径（Esc / × / 取消 / 完成）全部收敛到 closeChatBackgroundEditor()：非 commit
// 一律回滚到打开时的快照（乐观应用铁律，见维护说明 §九.69）。
let chatBgEditorSnapshot = null;
const chatBgEditorDerived = new Set();   // 本次编辑中旋转出来的派生图（取消时要回收）
let chatBgDrag = null;                   // { pointerId, x, y }
let chatBgResize = null;                 // { pointerId, x, y }
let chatBgPinch = null;                  // { distance, wf }
const chatBgPointers = new Map();

// 派生图回收：服务端在"文件有引用/不在 uploads"时会拒，属预期，不影响其它流程。
function reclaimBackgroundFile(path) {
  const target = String(path || '');
  if (!target) return;
  api('/api/uploads/delete', { method: 'POST', body: { path: target } }).catch(() => { /* 交给清理机制 */ });
}

// 是不是内置背景图：它们在 data/backgrounds（不在 uploads）里，既不该删、也删不掉
// （回收请求会被服务端 403 拒掉，只在控制台留一条没意义的 4xx）。所以先按清单排除。
function isBuiltInBackground(path) {
  const target = String(path || '');
  if (!target) return false;
  return (state.chatBackgroundPresets || []).some((item) => String(item.path || '') === target);
}

// 概览里"整张图"的显示尺寸（px）：拖动位移要换算成图片比例位移，就靠它。
function chatBgOverviewMetrics() {
  const rect = $('#chatBgOverviewImage')?.getBoundingClientRect?.();
  if (!rect || !rect.width || !rect.height) return null;
  return { width: rect.width, height: rect.height };
}

// 拖取景框 = 平移取景区域：Δpx → Δ图片比例 → 写回 crop。
// 两个方向都自由，唯一的约束是"框不许出图"（clampChatBackgroundCrop）——旧模型那种
// "某个方向自由度恰好为 0、拖了没反应"的状态在数据层已经不可能出现。
function moveChatBackgroundFrameBy(dxPx, dyPx) {
  const metrics = chatBgOverviewMetrics();
  if (!metrics) return;
  const crop = chatBackgroundCrop(state.chatBackground || {});
  applyChatBackground({
    crop: clampChatBackgroundCrop({
      ...crop,
      x: crop.x + dxPx / metrics.width,
      y: crop.y + dyPx / metrics.height,
    }),
  });
  updateChatBackgroundEditorControls();
}

// 拖手柄改大小：handle = n / s / e / w 的任意组合（角 = 两个方向一起改）。
// **这才是"自由裁剪"**：横向能切、竖向也能切，框形状不受对话区比例约束（旧模型锁死比例，
// 于是"填满/完整显示"必然有一个方向切不动）。手柄拖到图片边界或最小边长就停住。
function resizeChatBackgroundFrameBy(handle, dxPx, dyPx) {
  const metrics = chatBgOverviewMetrics();
  if (!metrics) return;
  const crop = chatBackgroundCrop(state.chatBackground || {});
  const dx = dxPx / metrics.width;
  const dy = dyPx / metrics.height;
  let { x, y, w, h } = crop;
  const right = x + w;
  const bottom = y + h;
  if (handle.includes('w')) {
    const nextX = Math.min(right - CHAT_BACKGROUND_MIN_CROP, Math.max(0, x + dx));
    w = right - nextX;
    x = nextX;
  }
  if (handle.includes('e')) {
    w = Math.min(1 - x, Math.max(CHAT_BACKGROUND_MIN_CROP, w + dx));
  }
  if (handle.includes('n')) {
    const nextY = Math.min(bottom - CHAT_BACKGROUND_MIN_CROP, Math.max(0, y + dy));
    h = bottom - nextY;
    y = nextY;
  }
  if (handle.includes('s')) {
    h = Math.min(1 - y, Math.max(CHAT_BACKGROUND_MIN_CROP, h + dy));
  }
  applyChatBackground({ crop: { x, y, w, h } });
  updateChatBackgroundEditorControls();
}

// 等比缩放取景框（滚轮 / 滑杆 / 双指捏合）：绕框心缩放，**不改形状**——改形状靠拖手柄。
// 上下限由"最小边长 + 不许出图"同时给出，到了边界就停住（不做隐式位移，免得手感发飘）。
function scaleChatBackgroundFrameTo(targetScale) {
  const current = chatBackgroundCrop(state.chatBackground || {});
  const limits = chatBackgroundCropScaleLimits();
  const from = chatBackgroundCropScale(current);
  if (!(from > 0)) return;
  const target = Math.min(limits.max, Math.max(limits.min, Number(targetScale) || from));
  const factor = from / target;
  const width = current.w * factor;
  const height = current.h * factor;
  const centerX = current.x + current.w / 2;
  const centerY = current.y + current.h / 2;
  applyChatBackground({
    crop: clampChatBackgroundCrop({
      x: centerX - width / 2,
      y: centerY - height / 2,
      w: width,
      h: height,
    }),
  });
  updateChatBackgroundEditorControls();
}

// 「填满 / 完整显示 / 复位」：一键换取景区域（复位 = 回到打开编辑器时的取景，不是出厂值）。
function applyChatBackgroundCropPreset(crop) {
  applyChatBackground({ crop: clampChatBackgroundCrop(crop) });
  updateChatBackgroundEditorControls();
}

// 「旋转 90°」：在服务端把（背景）图顺时针转 90° 另存为新文件，再把它设为背景。
// 属于"换图"而不是"取景"：立刻落库（与选图/预设同一通道），失败整份回滚；
// 取消编辑器时把它们回收掉——原图全程不动。
async function rotateChatBackgroundImage() {
  const current = String(state.chatBackground?.image || '');
  if (!current) return;
  const button = $('#chatBgRotate');
  if (button) button.disabled = true;
  const before = { ...(state.chatBackground || {}) };
  setChatBackgroundEditorError('');
  setChatBackgroundStatus('正在旋转…');
  try {
    const result = await api('/api/uploads/rotate', { method: 'POST', body: { path: current, turns: 1 } });
    if (!result?.path) throw new Error('旋转接口没有返回新文件');
    // 取景跟着图一起转：顺时针 90° 把 (x, y, w, h) 映射成 (1-y-h, x, h, w)。
    // 必须**在探新比例之前**读出来——旧模型（crop 缺失）的换算要用旧几何。
    const cropBefore = chatBackgroundCrop(state.chatBackground || {});
    chatBgEditorDerived.add(String(result.path));
    await probeChatBackgroundImageAspect(result.path);
    await saveChatBackground({
      image: result.path,
      crop: clampChatBackgroundCrop({
        x: 1 - cropBefore.y - cropBefore.h,
        y: cropBefore.x,
        w: cropBefore.h,
        h: cropBefore.w,
      }),
      position_x: 50,
      position_y: 50,
      zoom: 1,
    });
    // 图片比例变了 → 概览与取景框要按新比例重画（滑杆上下限也跟着变）。
    populateChatBackgroundEditor();
    setChatBackgroundStatus('已顺时针旋转 90°（取景跟着转过去了，可继续调）');
  } catch (error) {
    applyChatBackground(before);
    updateChatBackgroundEditorControls();
    setChatBackgroundEditorError(`旋转失败：${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

async function openChatBackgroundEditor() {
  const dialog = $('#chatBackgroundDialog');
  const frame = $('#chatBgCropFrame');
  if (!dialog || !frame) return;
  const image = String(state.chatBackground?.image || '');
  if (!image) {
    setChatBackgroundStatus('先选择一张背景图，再调整取景', true);
    return;
  }
  // 打开前刷新几何量：编辑器打开期间不再跟踪窗口变化（对话区比例与滑杆上下限定格，
  // 免得调图中窗口一动取景就跳；关掉重开即精确）。
  refreshChatBackgroundGeometry();
  const aspect = await probeChatBackgroundImageAspect(image);
  chatBgEditorDerived.clear();
  chatBgEditorSnapshot = { ...(state.chatBackground || {}) };
  if (!dialog.open) dialog.showModal();
  // 躯干是可滚动的：重开时必须回到顶部，否则停在上次滚到的位置（实测：概览被滚出可视区，
  // 拖框 / 拖动都抓不到它）。
  const body = $('#chatBackgroundDialog .chat-bg-body');
  if (body) body.scrollTop = 0;
  populateChatBackgroundEditor();
  if (!aspect) {
    // 图片文件没了（缓存清理 / 换机器）：弹层里说清楚并锁住控件，不显示一块空白概览。
    setChatBackgroundEditorEnabled(false);
    setChatBackgroundEditorError('背景图文件已失效，请重新选择图片');
  }
  frame.focus({ preventScroll: true });
}

// 非 commit 的关闭：回滚到打开时的取景，并回收本次编辑中旋转出来的派生图。
// 注意"回滚"必须**同时落库**：旋转是立刻落库的换图，只回滚本地的话刷新又变回旋转后的图
// （实测被校验脚本抓到：取消后服务端仍是 _rot90 那张）。回收要等落库之后再做，
// 否则那一刻文件仍被设置引用，服务端会以 409 拒绝（还会在控制台留一条 4xx）。
async function revertChatBackgroundEditor() {
  const snapshot = chatBgEditorSnapshot;
  chatBgEditorSnapshot = null;
  const derived = [...chatBgEditorDerived];
  chatBgEditorDerived.clear();
  if (!snapshot) return;
  const imageChanged = derived.length > 0
    || String(snapshot.image || '') !== String(state.chatBackground?.image || '');
  applyChatBackground(snapshot);
  if (imageChanged) {
    try {
      await saveChatBackground(snapshot);
    } catch (error) {
      toast(`撤销旋转失败：${error.message}`);
      return;   // 落库失败就别回收派生图：它可能还被服务端引用着
    }
  }
  derived.filter((path) => path && path !== snapshot.image).forEach(reclaimBackgroundFile);
}

function closeChatBackgroundEditor({ commit = false } = {}) {
  const dialog = $('#chatBackgroundDialog');
  if (commit) chatBgEditorSnapshot = null;
  else revertChatBackgroundEditor();
  if (dialog?.open) dialog.close();
  else updateChatBackgroundControls();
}

async function commitChatBackgroundEditor() {
  const background = state.chatBackground || {};
  const original = String(chatBgEditorSnapshot?.image || '');
  try {
    // 落库的是取景区域本身（crop；null = 自动）。旧字段不再回写——它们是升级换算的输入，
    // 由服务端保留原值即可。
    await saveChatBackground({ crop: background.crop ?? null });
  } catch (error) {
    // 保存失败就留在弹层里，快照不丢（之后按取消仍能回滚），也不假装已保存。
    setChatBackgroundEditorError(`保存失败：${error.message}`);
    return;
  }
  chatBgEditorDerived.clear();
  // 编辑期间换过图（旋转）：旧图不再被引用 → 回收（与"选图/换图"同一条规则；
  // 内置背景图不在 uploads 里，跳过）。
  if (original && original !== String(background.image || '') && !isBuiltInBackground(original)) {
    reclaimBackgroundFile(original);
  }
  closeChatBackgroundEditor({ commit: true });
  setChatBackgroundStatus('背景取景已保存');
}

function bindChatBackgroundEditor() {
  const dialog = $('#chatBackgroundDialog');
  const frame = $('#chatBgCropFrame');
  const overview = $('#chatBgOverview');
  const preview = $('#chatBackgroundPreview');
  $('#editChatBackground')?.addEventListener('click', () => { void openChatBackgroundEditor(); });
  // 缩略图本身就是入口：它已经与真实背景同源，点它进编辑器是最自然的心智。
  preview?.addEventListener('click', () => { void openChatBackgroundEditor(); });
  preview?.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    void openChatBackgroundEditor();
  });
  if (!dialog || !frame || !overview) return;

  // close 事件是**唯一兜底**：Esc、× 、取消、以及任何 dialog.close() 都会走到这里。
  dialog.addEventListener('close', () => {
    if (chatBgEditorSnapshot) void revertChatBackgroundEditor();
    chatBgPointers.clear();
    chatBgDrag = null;
    chatBgResize = null;
    chatBgPinch = null;
    frame.classList.remove('is-dragging');
    updateChatBackgroundControls();
  });

  $('#chatBgSave')?.addEventListener('click', () => { void commitChatBackgroundEditor(); });
  // 「填满」= 图片内最大的、比例等于对话区比例的居中矩形（= 旧模型 zoom=1 的观感）。
  $('#chatBgFitCover')?.addEventListener('click', () => applyChatBackgroundCropPreset(chatBackgroundFillCrop()));
  // 「完整显示」= 整张图（形状与对话区不同就留白，由模糊底补）。
  $('#chatBgFitContain')?.addEventListener('click', () => applyChatBackgroundCropPreset({ x: 0, y: 0, w: 1, h: 1 }));
  // 「复位」= 回到打开编辑器时的取景（"我刚调乱了，退回去"），不是出厂值。
  $('#chatBgReset')?.addEventListener('click', () => {
    applyChatBackgroundCropPreset(chatBgEditorSnapshot?.crop || chatBackgroundFillCrop());
  });
  $('#chatBgRotate')?.addEventListener('click', () => { void rotateChatBackgroundImage(); });
  $('#chatBgZoom')?.addEventListener('input', (event) => scaleChatBackgroundFrameTo(event.target.value));

  // 双指捏合只在**真·多指**（触屏/笔）时成立：鼠标 + 表里残留的指针曾被误判成捏合，
  // 表现就是"只能缩放、拖不动"（见下面 pointerdown 里的自愈注释）。
  const startPinch = () => {
    const touchPointers = [...chatBgPointers.values()].filter((pointer) => pointer.type !== 'mouse');
    if (touchPointers.length < 2) return false;
    const [first, second] = touchPointers;
    chatBgPinch = {
      distance: Math.hypot(first.x - second.x, first.y - second.y),
      scale: chatBackgroundCropScale(chatBackgroundCrop(state.chatBackground || {})),
    };
    return true;
  };
  const endPointer = (event) => {
    chatBgPointers.delete(event.pointerId);
    if (chatBgPointers.size < 2) chatBgPinch = null;
    if (chatBgDrag?.pointerId === event.pointerId) {
      chatBgDrag = null;
      frame.classList.remove('is-dragging');
    }
    if (chatBgResize?.pointerId === event.pointerId) chatBgResize = null;
  };

  // 手势统一挂在**概览**上：手柄、框内、框外的压暗区都能起手。
  // 为什么不在框上监听：命中区越大越不容易"拖不动"（用户曾因"只能缩放、动不了"报障）；
  // 现在两个方向的自由度由 crop 模型保证——框永远在图片内，横向/竖向都**永远**有空间。
  overview.addEventListener('pointerdown', (event) => {
    if (event.button) return;
    // 新手势开始：主指针按下先清残留。丢了 pointerup（捕获失败、在窗口外松手、切窗口回来）
    // 会在表里留一个"幽灵指针"，下一次按下就会被当成双指捏合——这正是"取景框锁死、
    // 只能缩放、不能上下左右移动"的根因。自愈比祈祷事件齐全可靠。
    if (event.isPrimary) {
      chatBgPointers.clear();
      chatBgDrag = null;
      chatBgResize = null;
      chatBgPinch = null;
    }
    // 捕获失败不能中断手势（合成指针 / 指针已释放时会抛 NotFoundError）：拿不到捕获只是
    // 少了"指针移出元素也收得到事件"的保障，底下还有 window 兜底，不该让整个拖拽起不来。
    try {
      overview.setPointerCapture?.(event.pointerId);
    } catch (_) { /* 自愈与兜底已覆盖 */ }
    chatBgPointers.set(event.pointerId, {
      x: event.clientX,
      y: event.clientY,
      type: event.pointerType || 'mouse',
    });
    const handle = event.target?.dataset?.chatBgHandle || '';
    if (startPinch()) {
      chatBgDrag = null;
      chatBgResize = null;
    } else if (handle) {
      chatBgResize = { pointerId: event.pointerId, handle, x: event.clientX, y: event.clientY };
    } else {
      chatBgDrag = { pointerId: event.pointerId, x: event.clientX, y: event.clientY };
      frame.classList.add('is-dragging');
      frame.focus({ preventScroll: true });
    }
    event.preventDefault();
  });
  overview.addEventListener('pointermove', (event) => {
    if (!chatBgPointers.has(event.pointerId)) return;
    chatBgPointers.set(event.pointerId, {
      x: event.clientX,
      y: event.clientY,
      type: event.pointerType || 'mouse',
    });
    if (chatBgPinch) {
      const touchPointers = [...chatBgPointers.values()].filter((pointer) => pointer.type !== 'mouse');
      if (touchPointers.length < 2 || !(chatBgPinch.distance > 0)) return;
      const [first, second] = touchPointers;
      const distance = Math.hypot(first.x - second.x, first.y - second.y);
      // 双指：按距离比缩放（与滑杆同一条路径）。
      scaleChatBackgroundFrameTo(chatBgPinch.scale * (distance / chatBgPinch.distance));
      return;
    }
    if (chatBgDrag && chatBgDrag.pointerId === event.pointerId) {
      moveChatBackgroundFrameBy(event.clientX - chatBgDrag.x, event.clientY - chatBgDrag.y);
      chatBgDrag.x = event.clientX;
      chatBgDrag.y = event.clientY;
      return;
    }
    if (!chatBgResize || chatBgResize.pointerId !== event.pointerId) return;
    resizeChatBackgroundFrameBy(
      chatBgResize.handle,
      event.clientX - chatBgResize.x,
      event.clientY - chatBgResize.y,
    );
    chatBgResize.x = event.clientX;
    chatBgResize.y = event.clientY;
  });
  overview.addEventListener('pointerup', endPointer);
  overview.addEventListener('pointercancel', endPointer);
  overview.addEventListener('lostpointercapture', endPointer);
  // 兜底：指针在窗口外抬起 / 系统取消时，pointerup 可能到不了概览。上面那次自愈能兜住，
  // 但直接收干净更省心（endPointer 幂等，重复触发无害）。
  window.addEventListener('pointerup', endPointer);
  window.addEventListener('pointercancel', endPointer);

  // 滚轮缩放框（绕框心）；指数手感：向上滚（deltaY < 0）放大。
  overview.addEventListener('wheel', (event) => {
    event.preventDefault();
    const scale = chatBackgroundCropScale(chatBackgroundCrop(state.chatBackground || {}));
    scaleChatBackgroundFrameTo(scale * Math.exp(-event.deltaY * 0.0015));
  }, { passive: false });

  // 方向键微调取景框：1px 步进，Shift 10px。
  frame.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? 10 : 1;
    const moves = {
      ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step],
    };
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    moveChatBackgroundFrameBy(move[0], move[1]);
  });
}

// ---- 应用图标（侧栏底部齿轮右侧入口）----
// 图标只影响桌面端外观（窗口标题栏/任务栏 + 系统托盘）：pywebview 不支持运行时换窗口图标，
// 因此与「启用手机访问」「数据目录迁移」同一种生效语义——**上传后完全退出并重新启动**才生效，
// 不做运行中即时换（只换托盘不换窗口就成了半截状态）。
// 预览图必须挂 token：`<img src>` 带不了 Authorization 头，手机/局域网访问靠查询串鉴权，
// 与 01-core 的 localFileUrl 同一口径。
function appIconImageUrl() {
  const token = state.token ? `token=${encodeURIComponent(state.token)}&` : '';
  return `/api/app-icon/image?${token}t=${Date.now()}`;
}

function setAppIconError(message) {
  const node = $('#appIconError');
  if (!node) return;
  node.textContent = message || '';
  node.hidden = !message;
}

function renderAppIconStatus(custom) {
  const status = $('#appIconStatus');
  if (status) status.textContent = custom ? '正在使用自定义图标' : '正在使用默认图标';
  const reset = $('#appIconReset');
  if (reset) reset.hidden = !custom;
  const preview = $('#appIconPreview');
  if (preview) preview.src = appIconImageUrl();
}

async function refreshAppIconDialog() {
  try {
    const payload = await api('/api/app-icon');
    renderAppIconStatus(Boolean(payload.custom));
  } catch (error) {
    setAppIconError(`读取图标状态失败：${error.message}`);
  }
}

async function uploadAppIcon(file) {
  if (!file) return;
  setAppIconError('');
  const form = new FormData();
  form.append('file', file, file.name || 'icon.png');
  try {
    const payload = await api('/api/app-icon', { method: 'POST', body: form });
    renderAppIconStatus(Boolean(payload.custom));
    toast('图标已更换，请完全退出并重新启动 Cat Chat 生效');
  } catch (error) {
    setAppIconError(`更换图标失败：${error.message}`);
  }
}

async function resetAppIcon() {
  setAppIconError('');
  try {
    const payload = await api('/api/app-icon', { method: 'DELETE' });
    renderAppIconStatus(Boolean(payload.custom));
    toast('已恢复默认图标，请完全退出并重新启动 Cat Chat 生效');
  } catch (error) {
    setAppIconError(`恢复默认失败：${error.message}`);
  }
}

function bindAppIconControls() {
  const entry = $('#openAppIcon');
  if (!entry) return; // 旧 index.html 没有该入口时整块跳过（与外观控件同款的可选增强约定）
  entry.addEventListener('click', () => {
    setAppIconError('');
    // 先给 <img> 一个 src 再开弹层：否则会先闪一下空框（alt 文本）才被接口回图盖上。
    const preview = $('#appIconPreview');
    if (preview) preview.src = appIconImageUrl();
    $('#appIconDialog').showModal();
    void refreshAppIconDialog();
  });
  $('#appIconPick').addEventListener('click', () => $('#appIconFile').click());
  $('#appIconFile').addEventListener('change', () => {
    const input = $('#appIconFile');
    const file = input.files && input.files[0];
    input.value = ''; // 选同一张图两次也要触发 change
    void uploadAppIcon(file);
  });
  $('#appIconReset').addEventListener('click', () => { void resetAppIcon(); });
}

// 最近一次 pointerdown 的指针类型：`contextmenu` 事件本身不带 pointerType，只能这样记下来。
// 用途：**触摸/手写笔长按必须让给系统菜单**。手机经局域网 http:// 打开时是**非安全上下文**，
// 网页既没有 `navigator.clipboard`、`execCommand('paste')` 也被浏览器禁用，系统长按菜单
// （复制 / 粘贴 / 全选）是手机上唯一可行的粘贴路径。旧实现在 contextmenu 上无条件
// `preventDefault()`，等于把这条路一起关掉——用户实测就是「粘贴失败：浏览器未授权」（§九.85）。
let lastPointerType = 'mouse';

function isLongPressPointer() {
  return lastPointerType === 'touch' || lastPointerType === 'pen';
}

// ---- 拖文件夹进输入区（桌面壳专属能力，见计划 B 部分）-----------------------------
// 为什么不能只看 `dataTransfer.files`：浏览器把"拖进来的目录"也塞进 files（一个 type 空、
// size 0 的假 File），照原样上传只会得到一个 0 字节附件。真实类型只能从 `webkitGetAsEntry()`
// 读——它必须在 drop 事件同步阶段取，事件对象出了处理器就失效。
const DESKTOP_FOLDER_WAIT_MS = 800;

let desktopFolderWaiter = null;   // 单次等待者：launcher 把路径送回来时兑现

// 同一批路径可能从两条通道回来（见 handleFolderDrop），按路径 + 时间做个去重，
// 免得一次拖拽长出两个一模一样的索引 chip。
const FOLDER_DROP_DEDUPE_MS = 5000;
const recentFolderPaths = new Map();

function isFreshFolderPath(path) {
  const now = Date.now();
  const last = recentFolderPaths.get(path) || 0;
  recentFolderPaths.set(path, now);
  if (recentFolderPaths.size > 64) {
    for (const [key, ts] of recentFolderPaths) {
      if (now - ts > FOLDER_DROP_DEDUPE_MS) recentFolderPaths.delete(key);
    }
  }
  return now - last > FOLDER_DROP_DEDUPE_MS;
}

/** 这次 drop 里有没有目录（按条目而非 files 判断）。 */
function hasDroppedDirectory(event) {
  const items = event.dataTransfer?.items || [];
  for (const item of items) {
    try {
      if (item.kind === 'file' && item.webkitGetAsEntry?.()?.isDirectory) return true;
    } catch (_) {
      // 老浏览器没有 webkitGetAsEntry：当作没有目录，走原有上传链路。
    }
  }
  return false;
}

/** 等 launcher 从旧通道（pywebview 的 drop 事件回调）把路径送回来；超时返回 null。 */
function waitDesktopFolderPaths() {
  if (!isPywebview()) return Promise.resolve(null);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      desktopFolderWaiter = null;
      resolve(value);
    };
    desktopFolderWaiter = finish;
    setTimeout(() => finish(null), DESKTOP_FOLDER_WAIT_MS);
  });
}

// 把"拖进来的文件对象"直接交给 WebView2 原生：**这是拿到硬盘真实路径的唯一入口**。
// 必须在这里同步调用（dataTransfer 出了事件就失效）。返回原生这一步的异常文本，
// 空字符串表示没抛异常。
//
// 为什么要自己调：pywebview 注入的 drop 监听也走这一步，但 WebView2 只要收到不受支持的
// 对象就抛异常、**整条消息作废**（官方文档），而它抛在页面里、Python 侧完全看不到——
// 于是"事件回调"这条路会静默死掉。自己调就能把异常抓在手里，并且配合
// `naibaFolderDrop`（直接读 pywebview 收到的路径）绕开那条死路。
function handFilesToNative(files) {
  try {
    const wv = window.chrome?.webview;
    if (!wv || typeof wv.postMessageWithAdditionalObjects !== 'function') {
      return 'no-postMessageWithAdditionalObjects';
    }
    if (!files || !files.length) return 'empty-filelist';
    wv.postMessageWithAdditionalObjects('FilesDropped', files);
    return '';
  } catch (error) {
    return `native-throw: ${(error && (error.message || error.name)) || error}`;
  }
}

/** 主动向桌面端要"这次拖进来的目录的真实路径"（不依赖 pywebview 的事件回调）。 */
async function requestDesktopFolderPaths(nativeError) {
  if (!isPywebview()) return { dirs: [], reason: 'browser' };
  try {
    const res = await window.pywebview.api.naibaFolderDrop(nativeError || '');
    const dirs = Array.isArray(res?.dirs) ? res.dirs.filter(Boolean) : [];
    return { dirs, reason: (res && res.reason) || 'noresult' };
  } catch (error) {
    return { dirs: [], reason: `api-failed: ${(error && error.message) || error}` };
  }
}

/** 建 chip（同一路径短时间内只建一次）。 */
async function addFolderPathsOnce(paths) {
  let added = 0;
  for (const path of paths) {
    if (!isFreshFolderPath(path)) continue;
    await addFolderChip(path, { quiet: true });
    added += 1;
  }
  return added;
}

async function handleFolderDrop(event, nativeError) {
  // 混合拖入（文件 + 文件夹）：普通文件仍走原链路，目录另行处理。
  const plainFiles = [...(event.dataTransfer?.files || [])].filter((file) => {
    try {
      return !(file.size === 0 && !file.type && !String(file.name || '').includes('.'));
    } catch (_) {
      return true;
    }
  });
  if (plainFiles.length) uploadFiles(plainFiles);
  // 两条通道一起等：① 主动追问（新，主通道）② 旧通道回交（launcher 直接回推，见
  // naibaHandleDroppedFolders）。旧的留着是因为它便宜，且"没有等待者时会自己建 chip"。
  const [waited, asked] = await Promise.all([
    waitDesktopFolderPaths(),
    requestDesktopFolderPaths(nativeError),
  ]);
  const added = await addFolderPathsOnce([...(waited || []), ...asked.dirs]);
  if (added > 0) return;
  // 拿不到路径：宁可明确说清，也不能把目录当文件传上去（只会得到一个 0 字节附件）。
  toast(
    isPywebview()
      ? '没能读到这个文件夹的位置，请用输入框的「@」选择文件夹'
      : '浏览器里拖文件夹拿不到完整路径：请在桌面客户端里拖入，或用输入框的「@」选择文件夹'
  );
}

// launcher 的 drop 事件回调（pywebview DOM 事件）把目录绝对路径从这里交回来。
// 浏览器端永远不会被调用——Chromium 不给绝对路径（安全模型），那里只走上面的降级提示。
window.naibaHandleDroppedFolders = (paths) => {
  const list = (Array.isArray(paths) ? paths : []).map((item) => String(item || '')).filter(Boolean);
  const waiter = desktopFolderWaiter;
  if (waiter) {
    waiter(list);
    return;
  }
  // 没有等待者（前端没识别出目录、或事件顺序反了）：直接建 chip，别丢用户的操作。
  void addFolderPathsOnce(list);
};

export function bindEvents() {
  document.addEventListener('contextmenu', (event) => {
    hideTextContextMenu();
    const editable = editableElement(event.target);
    if (editable) {
      // 长按（触摸 / 手写笔）不拦截：交给系统自己的文本菜单，见 lastPointerType 的注释。
      if (isLongPressPointer()) return;
      event.preventDefault();
      showTextContextMenu(event, '', 'edit', editable);
      return;
    }
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.toString().trim()) return;
    const range = selection.getRangeAt(0);
    const startNode = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer : range.startContainer.parentElement;
    const endNode = range.endContainer.nodeType === Node.ELEMENT_NODE
      ? range.endContainer : range.endContainer.parentElement;
    const messageBody = startNode?.closest('.message-body');
    if (!messageBody || endNode?.closest('.message-body') !== messageBody) return;
    // 模态弹层不清除 window.getSelection()：设置页里右键会拿着底层会话的残留选区，
    // 弹出"复制选中/快速发送"（会话页菜单）。这里把选区菜单限制在同一弹层内。
    const dialog = event.target.closest?.('dialog[open]');
    if (dialog && !dialog.contains(messageBody)) return;
    if (!dialog && topLayerContainer() !== document.body) return;
    event.preventDefault();
    showTextContextMenu(event, selection.toString(), 'selection', null);
  });
  // 记录指针类型（捕获阶段：即使后续 handler 停掉传播也要先记到）。只记不拦，行为零影响。
  document.addEventListener('pointerdown', (event) => {
    lastPointerType = event.pointerType || 'mouse';
  }, true);
  document.addEventListener('pointerdown', (event) => {
    if (!event.target.closest?.('#textContextMenu')) hideTextContextMenu();
  });
  const textContextMenu = ensureContextMenu();
  textContextMenu.addEventListener('mousedown', (event) => {
    // 阻止菜单按钮抢走文本框焦点，保持选中状态可见。
    if (event.target.closest?.('[data-context-action]')) event.preventDefault();
  });
  textContextMenu.addEventListener('click', (event) => {
    const button = event.target.closest('[data-context-action]');
    if (button) runTextContextAction(button.dataset.contextAction);
  });
  textContextMenu.addEventListener('keydown', (event) => {
    const buttons = [...textContextMenu.querySelectorAll('button:not(:disabled)')];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      hideTextContextMenu();
      contextMenuPreviousFocus?.focus?.({ preventScroll: true });
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const offset = event.key === 'ArrowDown' ? 1 : -1;
      buttons[(index + offset + buttons.length) % buttons.length]?.focus();
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      buttons[event.key === 'Home' ? 0 : buttons.length - 1]?.focus();
    }
  });
  window.addEventListener('blur', hideTextContextMenu);
  window.addEventListener('resize', hideTextContextMenu);
  window.addEventListener('scroll', hideTextContextMenu, true);
  // 大图右键 → 复制图片剪贴板（仅 pywebview 窗口自绘菜单；真实浏览器保留原生“复制图片”菜单）。
  document.addEventListener('contextmenu', (event) => {
    if (!isPywebview()) return;
    if (!event.target.closest?.('#imageLightboxImg')) return;
    event.preventDefault();
    showImageContextMenu(event);
  });
  document.addEventListener('pointerdown', (event) => {
    if (!event.target.closest?.('#imageContextMenu')) hideImageContextMenu();
  });
  const imageMenu = ensureImageContextMenu();
  imageMenu.addEventListener('contextmenu', (event) => event.preventDefault());
  imageMenu.addEventListener('click', (event) => {
    const button = event.target.closest('[data-image-context-action]');
    if (button) runImageContextAction(button.dataset.imageContextAction);
  });
  imageMenu.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.preventDefault(); hideImageContextMenu(); }
  });
  window.addEventListener('blur', hideImageContextMenu);
  window.addEventListener('resize', hideImageContextMenu);
  window.addEventListener('scroll', hideImageContextMenu, true);
  $('#authForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await authenticate($('#tokenInput').value.trim());
      $('#authDialog').close();
      await initialize();
    } catch (error) {
      $('#authError').textContent = error.message;
    }
  });
  $('#modelSelect').addEventListener('change', saveModelSelection);
  $('#composerModelSelect').addEventListener('change', saveComposerModelSelection);
  // 自动化脚本 / 程序化改值（page.selectOption）不经过浮层：change 后把触发按钮文案对齐一次。
  $('#composerModelSelect').addEventListener('change', () => syncComposerModelPicker());
  $('#agentSelect').addEventListener('change', saveAgentSelection);
  $('#openSkills').addEventListener('click', () => $('#skillsDialog').showModal());
  $('#openTasks').addEventListener('click', () => $('#tasksDialog').showModal());
  // 显式刷新按钮：适配 EXE 内嵌 pywebview 无法使用 F5 的场景，EXE 与浏览器通用。
  $('#reloadPage')?.addEventListener('click', () => reloadPage());
  // 手机端顶栏折叠条（桌面不渲染）：按当前状态取反，收起/展开操作区。
  $('#toggleTopbarCompact')?.addEventListener('click', () => {
    const topbar = $('.topbar');
    setTopbarCompact(!(topbar && topbar.classList.contains('compact')));
  });
  $('#clearTerminalTasks').addEventListener('click', clearTerminalTasks);
  $('#activeTaskBar').addEventListener('click', (event) => {
    if (event.target.closest('[data-open-tasks]')) $('#tasksDialog').showModal();
  });
  // 审批模式上拉框：触发按钮负责开合，选项按钮才是选择（点选后由 switchPermissionMode 收起）。
  $('#permissionModeButton').addEventListener('click', (event) => {
    event.stopPropagation();
    togglePermissionModeMenu();
  });
  $('#permissionModeMenu').addEventListener('click', (event) => {
    const option = event.target.closest('[data-permission-mode]');
    if (option && !option.disabled) switchPermissionMode(option.dataset.permissionMode);
  });
  window.addEventListener('resize', positionPermissionModeMenu);
  window.addEventListener('scroll', positionPermissionModeMenu, true);
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !permissionMenuState.open) return;
    closePermissionModeMenu();
    $('#permissionModeButton').focus();
  });
  // 输入区模型「可搜索下拉」：触发按钮开合 + 面板内点击委托 + 搜索过滤 + 键盘选择。
  // 浮层打开时会挂到 body（fixed 定位），所以查找一律按 id，不用后代选择器。
  $('#composerModelTrigger')?.addEventListener('click', (event) => {
    event.stopPropagation();
    toggleComposerModelPicker();
  });
  $('#composerModelPanel')?.addEventListener('click', handleComposerModelPickerClick);
  $('#composerModelSearch')?.addEventListener('input', (event) => filterComposerModelPicker(event.target.value));
  $('#composerModelSearch')?.addEventListener('keydown', handleComposerModelPickerKey);
  window.addEventListener('resize', positionComposerModelPicker);
  window.addEventListener('scroll', positionComposerModelPicker, true);
  document.addEventListener('keydown', (event) => {
    // 搜索框内的 Esc 由 handleComposerModelPickerKey 消费（已 stopPropagation）；
    // 这里兜住「焦点不在搜索框」时的关闭（如点过列表项之后）。
    if (event.key !== 'Escape' || !composerPickerState.open) return;
    closeComposerModelPicker();
    $('#composerModelTrigger')?.focus();
  });
  // 任务卡片上只有三个显式动作（停止 / 详情 / 跳转），按「破坏性优先」判定；
  // 卡片本体不是动作区——点任务名、说明、空白一律无副作用（此前整卡可点，读日志或
  // 选中文本时一次误触就切走会话并顺手关掉面板）。所以这里不需要兜底跳转分支。
  $('#taskList').addEventListener('click', (event) => {
    const cancelButton = event.target.closest('[data-task-cancel]');
    if (cancelButton) {
      event.stopPropagation();
      cancelTask(cancelButton.dataset.taskCancel);
      return;
    }
    const detailButton = event.target.closest('[data-task-detail]');
    if (detailButton) {
      event.stopPropagation();
      const item = detailButton.closest('[data-task-id]');
      const panel = item?.querySelector('.task-detail');
      const log = item?.querySelector('.task-log');
      // 展开态以日志框为准：详情行可能为空（无结构化字段），日志容器则常驻。
      const open = log ? log.hidden : Boolean(panel?.hidden);
      if (panel) panel.hidden = !open;
      // 详情面板与任务日志同开同关（日志是这个折叠块的正文，不该再多一个按钮）。
      if (log) log.hidden = !open;
      detailButton.setAttribute('aria-expanded', String(open));
      detailButton.textContent = open ? '收起' : '详情';
      // 展开即拉一次日志；任务还在跑时由任务列表轮询持续续拉（cursor 只取新增行）。
      setTaskLogOpen(detailButton.dataset.taskDetail, open);
      return;
    }
    const openButton = event.target.closest('[data-task-open]');
    if (!openButton) return;
    event.stopPropagation();
    const task = state.tasks.find((value) => value.id === openButton.dataset.taskOpen);
    const conversationId = String(task?.conversation_id || '');
    if (!conversationId) {
      toast('这个任务没有关联的对话');
      return;
    }
    $('#tasksDialog').close();
    // 与侧栏打开会话同口径：失败要出声（此前是未捕获的 Promise 拒绝）。
    openConversation(conversationId).catch((error) => toast(`打开会话失败：${error.message}`));
  });
  // 日志滚动位置影响「新行到来/整表重绘时贴不贴底」：用户上翻读历史时不能被拽回底部。
  // scroll 不冒泡，但捕获阶段会经过祖先容器，所以委托在 #taskList 上监听（第三个参数 true）。
  $('#taskList').addEventListener('scroll', (event) => {
    const lines = event.target?.closest?.('.task-log-lines');
    const item = lines?.closest?.('[data-task-id]');
    if (!lines || !item) return;
    setTaskLogStick(item.dataset.taskId, lines.scrollHeight - lines.scrollTop - lines.clientHeight < 12);
  }, true);
  $('#mcpStatus').addEventListener('click', () => {
    $('#settingsDialog').showModal();
    switchSettingsTab('connections');
  });
  // 顶栏「刷新」与「卸载模型」已回到操作区常驻（不再藏进「⋯」溢出菜单）。
  document.addEventListener('click', (event) => {
    // 会话「⋯」菜单：点击菜单与触发按钮之外的位置即关闭。
    const conversationMenu = $('#conversationItemMenu');
    if (conversationMenu && !conversationMenu.hidden
      && !conversationMenu.contains(event.target)
      && !event.target.closest?.('[data-action="open-conversation-menu"]')) {
      closeConversationMenu();
    }
  });
  $('#saveMcpServer')?.addEventListener('click', saveMcpServer);
  $('#openSettings').addEventListener('click', () => {
    $('#settingsDialog').showModal();
    loadStorageStats();
    refreshImageCacheSize();
    // 内置背景清单：懒加载一次（后端首次访问时幂等生成到 data/backgrounds/）。
    void loadChatBackgroundPresets();
  });
  // 外观面板控件为可选增强：旧版 index.html 没有这些节点时不影响其它事件。
  const appearanceReset = $('#resetAppearance');
  const appearanceSave = $('#saveAppearance');
  const setAppearanceStatus = (text) => {
    const status = $('#appearanceStatus'); if (status) status.textContent = text;
  };
  // 会话字体卡片在下一张卡片里，单独给一行反馈，用户不用回头看上面那张卡。
  const setFontStatus = (text) => {
    const status = $('#chatFontStatus'); if (status) status.textContent = text;
  };
  // 预览（本地即时生效，不落库）+ 回显控件。字号滑块拖动是高频事件，
  // 走这条路径就绝不会每像素打一次 API；落库统一由「保存外观」完成，
  // 与主题/皮肤保持同一种交互（面板里不出现"有的即时存、有的要点击"）。
  const previewAppearance = (dirty) => {
    applyAppearance(appearanceFormValues());
    syncAppearanceControls();
    if (dirty) setAppearanceStatus('有未保存的外观更改');
    if (dirty) setFontStatus('有未保存的会话字体更改');
  };
  $$('input[name="appearanceTheme"], input[name="appearanceSkin"], input[name="appearanceChatFont"]').forEach((input) => input.addEventListener('change', () => previewAppearance(true)));
  $('#chatFontSize')?.addEventListener('input', () => previewAppearance(true));
  // 点选字体（原生 select：手机上弹系统选择器，不必打字）；选到「手动填写…」时
  // syncAppearanceControls 会把文本框那一行展开，走的是同一条预览/落库路径。
  $('#chatFontPick')?.addEventListener('change', () => previewAppearance(true));
  $('#chatFontFamilyCustom')?.addEventListener('input', () => previewAppearance(true));
  appearanceSave?.addEventListener('click', async () => {
    try {
      await saveAppearance(appearanceFormValues());
      syncAppearanceControls();
      setAppearanceStatus('外观设置已保存');
      setFontStatus('会话字体已保存');
    } catch (error) { toast(`保存外观失败：${error.message}`); }
  });
  appearanceReset?.addEventListener('click', async () => {
    try {
      await saveAppearance({ theme: 'system', skin: 'violet', chat_font_size: 15, chat_font_family: 'system', chat_font_family_custom: '' });
      syncAppearanceControls();
      setAppearanceStatus('已恢复默认外观');
      setFontStatus('已恢复默认字体');
    } catch (error) { toast(`恢复默认失败：${error.message}`); }
  });
  // 聊天背景：选图（走 /api/uploads，不进待发送附件列表）、强度滑杆、清除。
  // 控件在旧 index.html 里不存在时整块跳过（与上面外观控件同款的可选增强约定）。
  bindChatBackgroundControls();
  // 背景图编辑器（白板取景）：入口、拖动/滚轮/双指、适配按钮、收口关闭。
  bindChatBackgroundEditor();
  // 应用图标（侧栏底部齿轮右侧）：弹窗预览 + 选择图片 / 恢复默认，重启生效。
  bindAppIconControls();
  $$('[data-close]').forEach((button) => button.addEventListener('click', () => $(`#${button.dataset.close}`).close()));
  // 删除后的撤销条：只有一个动作（撤销）。10 秒窗口与进度条时长由 04-messages 负责，
  // 这里只保证点得到——条本身是 body 上的常驻节点，不能每次删除都重新绑监听。
  $('#undoBarAction')?.addEventListener('click', () => { void undoLastDelete(); });
  // 会话条目「⋯」菜单：菜单项点击 → 重命名/删除；点击外部、Esc、侧栏滚动均关闭。
  $('#conversationItemMenu')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-conversation-action]');
    if (!button) return;
    const id = conversationMenuTargetId();
    closeConversationMenu();
    if (!id) return;
    if (button.dataset.conversationAction === 'rename') openRenameConversation(id);
    else if (button.dataset.conversationAction === 'delete') {
      deleteConversation(id).catch((error) => toast(`删除失败：${error.message}`));
    }
  });
  $('#renameConversationForm')?.addEventListener('submit', saveRenameConversation);
  $('#newWorkspaceForm')?.addEventListener('submit', saveNewWorkspace);
  // Esc 关闭 ⋯ 菜单：菜单本身不一定持有焦点（点击 ⋯ 后焦点在按钮上），
  // 因此挂在 document 上而不是菜单元素上。
  // 分支链面板（点会话行 ⑂ 徽标 / ⑂N 计数弹出）：点条目切会话；点外部 / Esc / 侧栏滚动收起。
  $('#branchChainList')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-branch-goto]');
    if (!button) return;
    const target = button.dataset.branchGoto || '';
    closeBranchChainPanel();
    if (target && target !== state.conversationId) {
      openConversation(target).catch((error) => toast(`打开会话失败：${error.message}`));
    }
  });
  $('#branchChainClose')?.addEventListener('click', closeBranchChainPanel);
  // 点面板与徽标之外收起。徽标自己负责开/关切换，这里必须放行——否则一次点击会
  // 先被 toggle 打开、再被这里立刻关掉（表现为「点了没反应」）。
  document.addEventListener('click', (event) => {
    const panel = $('#branchChainPanel');
    if (!panel || panel.hidden) return;
    if (panel.contains(event.target)) return;
    if (event.target.closest?.('[data-action="open-branch-chain"]')) return;
    closeBranchChainPanel();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const menu = $('#conversationItemMenu');
    if (menu && !menu.hidden) closeConversationMenu();
    const chain = $('#branchChainPanel');
    if (chain && !chain.hidden) closeBranchChainPanel();
  });
  // Agent 编辑表单：快捷提示词面板（套用 / × 删除）+ 另存 + 角色卡追加导入。
  $('#agentPromptPresetButton')?.addEventListener('click', (event) => {
    event.stopPropagation();
    void toggleAgentPromptPresetPanel();
  });
  $('#agentPromptPresetClose')?.addEventListener('click', closeAgentPromptPresetPanel);
  $('#agentPromptPresetPanel')?.addEventListener('click', handleAgentPromptPresetPanelClick);
  // 点击面板与按钮之外收起；面板挂在弹层内部，用 composedPath 判断是否点在面板里。
  // 编辑/另存弹窗（#promptPresetDialog）属于同一流程，点它不算"点外面"——否则从面板点 ✎
  // 进去改完保存，面板会在保存那一下被误关（冒烟实测）。
  document.addEventListener('click', (event) => {
    const panel = $('#agentPromptPresetPanel');
    if (!panel || panel.hidden) return;
    const path = event.composedPath ? event.composedPath() : [];
    if (path.includes(panel) || path.includes($('#agentPromptPresetButton'))
        || path.includes($('#promptPresetDialog'))) return;
    closeAgentPromptPresetPanel();
  });
  window.addEventListener('resize', positionAgentPromptPresetPanel);
  // 弹层里按 Esc：面板开着就先收面板，别把整个 Agent 弹层关掉。
  $('#agentDialog')?.addEventListener('cancel', (event) => {
    const panel = $('#agentPromptPresetPanel');
    if (panel && !panel.hidden) {
      event.preventDefault();
      closeAgentPromptPresetPanel();
    }
  });
  $('#saveAgentPromptPreset')?.addEventListener('click', openAgentPromptPresetSaveDialog);
  $('#promptPresetForm')?.addEventListener('submit', saveAgentPromptPreset);
  $('#importAgentCharacterCard')?.addEventListener('click', () => $('#agentCharacterCardFileInput')?.click());
  $('#agentCharacterCardFileInput')?.addEventListener('change', (event) => {
    const file = event.target.files?.[0];
    if (file) importAgentCharacterCard(file);
    event.target.value = '';
  });
  // 自定义头像：选图后本地预览，保存 Agent 时才上传（新建时还没有 id）。
  $('#pickAgentAvatar')?.addEventListener('click', pickAgentAvatar);
  $('#agentAvatarFileInput')?.addEventListener('change', (event) => {
    const file = event.target.files?.[0];
    if (file) handleAgentAvatarFile(file);
    event.target.value = '';
  });
  $('#skillSearch').addEventListener('input', (event) => renderSkills(event.target.value));
  $('#composerForm').addEventListener('submit', (event) => {
    event.preventDefault();
    // 输入区在编辑态下已移到消息处，提交同一表单时确认当前编辑。
    if (state.editingMessageId) { confirmActiveEdit(); return; }
    if (state.chatRunId || state.abortController) cancelCurrentRun();
    else sendMessage();
  });
  // 「插话」按钮：运行中出现（可用性由 updateSendButtonState 单点维护），
  // 点了只是入队——真正的「现在就用上这条指令」是队列行上的「引导」。
  $('#interjectButton').addEventListener('click', () => { void sendRunInterjection(); });
  $('#messageInput').addEventListener('input', () => { resizeTextarea(); renderInputMirror(); updateSkillPopup(); updateFilePopup(); updateSendButtonState(); });
  $('#messageInput').addEventListener('select', updateSkillPopup);
  $('#messageInput').addEventListener('click', () => { updateSkillPopup(); updateFilePopup(); });
  $('#messageInput').addEventListener('focus', () => { updateSkillPopup(); updateFilePopup(); });
  $('#messageInput').addEventListener('scroll', () => { const mirror = $('#inputMirror'); if (mirror) mirror.scrollTop = $('#messageInput').scrollTop; positionSkillPopup(); positionFilePopup(); });
  window.addEventListener('resize', () => { positionSkillPopup(); positionFilePopup(); });
  $('#messageInput').addEventListener('keydown', (event) => {
    // @ 工作区引用弹层优先消费按键（Tab 进目录 / Shift+Tab 返回 / Enter 引用）。
    if (handleFilePopupKey(event)) {
      if (event.key === 'Escape') event.stopPropagation();
      return;
    }
    if (popupState.open) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        moveSkillPopupSelection(event.key === 'ArrowDown' ? 1 : -1);
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        const sel = popupState.items[popupState.selectedIndex];
        if (sel) commitSkillSelection(sel);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        hideSkillPopup();
        return;
      }
    }
    if (event.key === 'Escape' && state.editingMessageId) {
      event.preventDefault();
      event.stopPropagation();
      cancelActiveEdit();
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      // 编辑态下回车同样确认编辑，与当前输入区的发送按钮保持一致。
      if (state.editingMessageId) { confirmActiveEdit(); return; }
      if (state.chatRunId || state.abortController) {
        // 运行中回车 = 入队插话（Ctrl/Cmd+回车 = 把队列里所有待引导项一次放行）。
        // 刻意不再是「回复进行中，请等待完成」——运行中打字本来就是合法输入通道。
        if (event.ctrlKey || event.metaKey) void guideAllInterjections();
        else void sendRunInterjection();
      } else {
        sendMessage();
      }
    }
  });
  $('#skillPopup').addEventListener('mousedown', (event) => event.preventDefault());
  $('#filePopup').addEventListener('mousedown', (event) => event.preventDefault());
  $('#filePopup').addEventListener('click', handleFilePopupClick);
  $('#skillPopup').addEventListener('click', (event) => {
    const button = event.target.closest('[data-skill-index]');
    if (!button) return;
    const sel = popupState.items[Number(button.dataset.skillIndex)];
    if (sel) commitSkillSelection(sel);
  });
  $('#skillPopup').addEventListener('mouseover', (event) => {
    const button = event.target.closest('[data-skill-index]');
    if (!button) return;
    const idx = Number(button.dataset.skillIndex);
    if (idx !== popupState.selectedIndex) setSkillPopupSelection(idx);
  });
  $('#skillList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-skill-insert]');
    if (!button) return;
    const skill = skillList().find((s) => s.id === button.dataset.skillInsert);
    if (!skill) return;
    insertSkillRefAtCursor(skill);
    renderInputMirror();
    $('#skillsDialog').close();
    $('#messageInput').focus();
  });
  $('#attachButton').addEventListener('click', () => $('#fileInput').click());
  // composer-meta「快捷消息」面板：按钮开合 + 点击委托（插入/编辑/删除/新建）+ 点外部关闭
  $('#quickMessageButton')?.addEventListener('click', (event) => {
    event.stopPropagation();
    toggleQuickMessagePanel();
  });
  $('#quickMessagePanel')?.addEventListener('click', handleQuickMessagePanelClick);
  window.addEventListener('resize', positionQuickMessagePanel);
  window.addEventListener('scroll', positionQuickMessagePanel, true);
  document.addEventListener('click', (event) => {
    // 审批上拉框：点菜单与触发按钮之外的地方即收起（触发按钮自身已 stopPropagation）。
    if (permissionMenuState.open && !event.target.closest?.('#permissionModeMenu')) closePermissionModeMenu();
    // 模型搜索浮层：点浮层与触发按钮之外即收起（触发按钮自身已 stopPropagation）。
    if (composerPickerState.open && !event.target.closest?.('#composerModelPanel')) closeComposerModelPicker();
    if (!quickPanelState.open) return;
    if (event.target.closest?.('#quickMessagePanel')) return;
    if (event.target.closest?.('#quickMessageButton')) return;
    if (event.target.closest?.('#starterPromptDialog')) return;
    closeQuickMessagePanel();
  });
  $('#fileInput').addEventListener('change', (event) => { uploadFiles([...event.target.files]); event.target.value = ''; });
  const composerWrap = document.querySelector('.composer-wrap');
  if (composerWrap) {
    composerWrap.addEventListener('dragover', (event) => {
      const types = event.dataTransfer?.types || [];
      const hasFiles = types.includes('Files');
      const hasPath = types.includes('application/x-naiba-file-path');
      if (!hasFiles && !hasPath) return;
      event.preventDefault();
      composerWrap.classList.add('dragover');
    });
    composerWrap.addEventListener('dragleave', (event) => {
      if (event.relatedTarget && composerWrap.contains(event.relatedTarget)) return;
      composerWrap.classList.remove('dragover');
    });
    composerWrap.addEventListener('drop', (event) => {
      const droppedPath = event.dataTransfer?.getData('application/x-naiba-file-path')
        || event.dataTransfer?.getData('text/uri-list');
      if (!event.dataTransfer?.files?.length && !droppedPath) return;
      event.preventDefault();
      composerWrap.classList.remove('dragover');
      if (hasDroppedDirectory(event)) {
        // 必须在同步阶段把 File 对象交给原生（dataTransfer 出了事件就失效），
        // 顺便把原生这一步的异常抓下来传给 Python 侧做诊断。
        const nativeError = handFilesToNative(event.dataTransfer?.files);
        void handleFolderDrop(event, nativeError);
        return;
      }
      if (event.dataTransfer.files?.length) uploadFiles([...event.dataTransfer.files]);
      else {
        const path = String(droppedPath).split('\n').find((item) => item && !item.startsWith('#')) || '';
        if (path) {
          const name = path.split(/[\\/]/).pop() || 'image';
          state.pendingFiles.push({ name, path, size: 0 });
          renderPendingFiles();
        }
      }
    });
  }
  // 记录用户是否停留在底部：流式输出时只有跟随底部才自动滚动，上滑阅读则不抢滚动。
  $('#messages').addEventListener('scroll', () => {
    setStickToBottom(isNearBottom());
  }, { passive: true });
  $('#messages').addEventListener('dragstart', (event) => {
    // 缩略图（带 data-large-url）由全局 dragstart 统一以"大图 URL"拖动，这里跳过避免重复设置。
    if (event.target.closest?.('[data-large-url]')) return;
    const image = event.target.closest?.('.attachment-image img, .media-image-link img');
    if (!image || !event.dataTransfer) return;
    const source = image.closest('a')?.href || image.src;
    event.dataTransfer.effectAllowed = 'copy';
    event.dataTransfer.setData('text/uri-list', source);
    event.dataTransfer.setData('text/plain', source);
    const cached = draggedFileCache.get(source);
    if (cached && event.dataTransfer.items?.add) {
      try { event.dataTransfer.items.add(cached); } catch (_error) { /* browser may reject cross-origin items */ }
    }
    if (cached) {
      try { event.dataTransfer.setData('DownloadURL', `${cached.type || 'application/octet-stream'}:${cached.name}:${source}`); } catch (_error) { /* optional Chrome hint */ }
    }
    const path = decodeURIComponent(new URL(source, location.href).searchParams.get('path') || '');
    if (path) event.dataTransfer.setData('application/x-naiba-file-path', path);
  });
  $('#deepReasoningButton').addEventListener('click', toggleDeepReasoning);
  $('#reasoningMenu')?.addEventListener('click', async (event) => {
    const effort = event.target.closest?.('[data-reasoning-effort]')?.dataset.reasoningEffort;
    if (effort) await setReasoningEffort(effort);
  });
  // 点空白处收起强度列表（与快捷消息面板同款交互）
  document.addEventListener('click', (event) => {
    if (event.target.closest?.('#reasoningMenu') || event.target.closest?.('#deepReasoningButton')) return;
    closeReasoningMenu();
  });
  window.addEventListener('resize', positionReasoningMenu);
  window.addEventListener('scroll', positionReasoningMenu, true);
  $('#contextUsageButton').addEventListener('click', toggleContextUsagePopover);
  $('#contextUsagePopover').addEventListener('click', (event) => event.stopPropagation());
  // 上下文提醒弹窗：「继续发送」走待续动作；任何关闭路径（Esc/✕/知道了）都要清掉待续动作。
  $('#contextWarningContinue').addEventListener('click', continueAfterContextWarning);
  $('#contextWarningDialog').addEventListener('close', resetContextWarningResume);
  window.addEventListener('resize', positionContextUsagePopover);
  window.addEventListener('scroll', positionContextUsagePopover, true);
  document.addEventListener('click', closeContextUsagePopover);
  // Agent 说明弹层（顶栏「?」）：与上下文用量弹层同一套开合规则。
  $('#agentHelpButton').addEventListener('click', toggleAgentHelpPopover);
  $('#agentHelpPopover').addEventListener('click', (event) => event.stopPropagation());
  window.addEventListener('resize', positionAgentHelpPopover);
  window.addEventListener('scroll', positionAgentHelpPopover, true);
  document.addEventListener('click', closeAgentHelpPopover);
  $('#messageInput').addEventListener('paste', handlePasteImage);
  $('#saveVision').addEventListener('click', saveVisionSettings);
  $('#testVision').addEventListener('click', testVisionConnection);
  $('#visionProvider').addEventListener('change', () => saveVisionSettings({ quiet: true }));
  $('#addVisionProvider').addEventListener('click', openVisionProviderForm);
  $('#deleteVisionProvider').addEventListener('click', deleteVisionProvider);
  $('#saveSearch').addEventListener('click', saveSearchSettings);
  $('#testSearch').addEventListener('click', testSearchConnection);
  $('#searchProfileSelect').addEventListener('change', (event) => {
    const profile = searchProfiles().find((item) => item.id === event.target.value) || {};
    renderSearchProfileFields(profile);
    $('#deleteSearchProfile').disabled = !event.target.value;
    if (event.target.value) {
      const profiles = searchProfiles();
      persistSearchProfiles(profiles, event.target.value, true).catch((error) => toast(`搜索 API 切换失败：${error.message}`));
    }
  });
  $('#addSearchProfile').addEventListener('click', addSearchProfile);
  $('#deleteSearchProfile').addEventListener('click', deleteSearchProfile);
  $('#pendingFiles').addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-file]');
    if (!button) return;
    const index = Number(button.dataset.removeFile);
    const [chip] = state.pendingFiles.splice(index, 1);
    // 上传中 → 中止 XHR；新上传附件 → 删除未引用的缓存文件。
    // 从历史消息带入的附件仅从编辑列表移除，取消编辑后原消息仍需使用原文件。
    if (chip?.cancel) chip.cancel();
    if (chip?.path && !chip.uploading && !chip.existingAttachment && !isFolderChip(chip)) {
      api('/api/uploads/delete', { method: 'POST', body: { path: chip.path } }).catch(() => { /* 有引用/删除失败时保留文件，由清理机制回收 */ });
    }
    renderPendingFiles();
  });

  $('#messages').addEventListener('click', async (event) => {
    const codeCopyButton = event.target.closest('[data-copy-code]');
    if (codeCopyButton) {
      const code = codeCopyButton.closest('.code-block')?.querySelector('code');
      if (!code) return;
      try {
        await copyText(code.textContent);
        codeCopyButton.textContent = '已复制';
        clearTimeout(codeCopyButton.copyResetTimer);
        codeCopyButton.copyResetTimer = setTimeout(() => {
          if (codeCopyButton.isConnected) codeCopyButton.textContent = '复制';
        }, 1500);
        toast('已复制代码');
      } catch (error) {
        toast(`复制失败：${error.message}`);
      }
      return;
    }
    const copyButton = event.target.closest('[data-copy-message]');
    if (copyButton) {
      const text = copyButton.closest('.message-body').querySelector('.answer-content')?.textContent || '';
      try {
        await copyText(text.trim());
        toast('已复制回复');
      } catch (error) {
        toast(`复制失败：${error.message}`);
      }
      return;
    }
    const regenerateButton = event.target.closest('[data-regenerate-message]');
    if (regenerateButton) {
      regenerateMessage(regenerateButton.dataset.regenerateMessage);
      return;
    }
    const choicePreviewToggle = event.target.closest('[data-choice-preview-toggle]');
    if (choicePreviewToggle) {
      // 正文里的「选择题」静态块默认折叠成一行，点开才展开选项（真正的答题入口是面板）
      const preview = choicePreviewToggle.closest('.choice-preview');
      if (preview) {
        const collapsed = preview.classList.toggle('is-collapsed');
        choicePreviewToggle.setAttribute('aria-expanded', String(!collapsed));
        const caret = choicePreviewToggle.querySelector('.choice-preview-caret');
        if (caret) caret.textContent = collapsed ? '▸' : '▾';
      }
      return;
    }
    const openFileButton = event.target.closest('[data-open-file]');
    if (openFileButton) {
      openFilePanel(openFileButton.dataset.openFile);
      return;
    }
    const branchButton = event.target.closest('[data-branch-message]');
    if (branchButton) {
      branchMessage(branchButton.closest('.message-row'));
      return;
    }
    const deleteButton = event.target.closest('[data-delete-message]');
    if (deleteButton) {
      void deleteMessageFlow(deleteButton.closest('.message-row'));
      return;
    }
    const sessionStartButton = event.target.closest('[data-session-start-after]');
    if (sessionStartButton) {
      startNewSession(sessionStartButton.dataset.sessionStartAfter);
      return;
    }
    const cancelSessionButton = event.target.closest('[data-cancel-session-start]');
    if (cancelSessionButton) {
      cancelSessionStart(cancelSessionButton.closest('.message-row')?.dataset.messageId);
      return;
    }
    const seedButton = event.target.closest('[data-fill-reset-seed]');
    if (seedButton) {
      const divider = seedButton.closest('.message-row.session-divider');
      let info = {};
      try {
        info = JSON.parse(divider?.dataset.resetSeedInfo || '{}');
      } catch (_error) {
        info = {};
      }
      if (fillContextResetSeed(info)) toast('种子消息已填入输入框，可编辑后发送');
      else toast('种子模板为空：请在「设置 → 运行设置」里填写或恢复默认');
      return;
    }
    const editButton = event.target.closest('[data-edit-message]');
    if (editButton) {
      startEditMessage(editButton.closest('.message-row'));
      return;
    }
  });
  $$('.starter-grid button').forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.installSkill) startSkillInstall();
    else if (button.dataset.editSkill) startSkillEdit();
    else if (button.id === 'starterAddBtn') openStarterPromptDialog();
    else if (button.dataset.prompt != null) sendMessage(button.dataset.prompt);
  }));
  $('#saveStarterPrompt').addEventListener('click', saveStarterPrompt);
  $('#starterRestoreBtn')?.addEventListener('click', restoreStarterPresets);
  $('#copyAddress').addEventListener('click', async () => {
    if (!state.bootstrap?.lan_enabled || !state.bootstrap?.lan_url) return;
    try {
      await copyText(state.bootstrap.lan_url);
      toast('手机访问地址已复制');
    } catch (error) {
      toast(`复制失败：${error.message}`);
    }
  });
  $('#enableLanAccess').addEventListener('click', enableLanAccess);
  $('#openSidebar').addEventListener('click', openSidebar);
  $('#closeSidebar').addEventListener('click', closeSidebar);
  $('#sidebarBackdrop').addEventListener('click', closeSidebar);
  $('#sidebarWorkspaceTree').addEventListener('click', onSidebarTreeClick);
  // 侧栏虚拟化：滚动时按窗口重绘可视行
  $('#sidebarWorkspaceTree').addEventListener('scroll', () => {
    closeConversationMenu();
    // 分支链面板是 fixed 挂在 body 上的浮层，锚点（会话行）滚走了它必须跟着收，
    // 否则会悬在半空指着一个已经不在那里的会话。
    closeBranchChainPanel();
    if (sidebarScrollRaf) return;
    setSidebarScrollRaf(requestAnimationFrame(() => {
      setSidebarScrollRaf(0);
      const tree = $('#sidebarWorkspaceTree');
      if (tree && sidebarRowCache.length) renderSidebarWindow(tree.scrollTop);
    }));
  }, { passive: true });
  // 滚轮步进接管：虚拟列表每次滚动都重写 innerHTML（配合 CSS overflow-anchor:none），
  // 原生"每格 100px"在这类容器上手感发滞；鼠标滚轮格按 1.5 倍（≈150px）接管，
  // 触控板的小步进仍交给浏览器原生，避免过度加速。
  $('#sidebarWorkspaceTree').addEventListener('wheel', (event) => {
    const tree = event.currentTarget;
    if (!tree || event.ctrlKey) return;
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? tree.clientHeight : 1;
    const delta = event.deltaY * unit;
    if (Math.abs(delta) < 40) return;
    const maxScroll = tree.scrollHeight - tree.clientHeight;
    if (maxScroll <= 0) return;
    const next = Math.max(0, Math.min(tree.scrollTop + delta * 1.5, maxScroll));
    if (next === tree.scrollTop) return;
    event.preventDefault();
    tree.scrollTop = next;
  }, { passive: false });
  // 右侧文件面板：标签页 / 正文操作 / 关闭 / Esc / 窗口宽度
  $('#fileTabs').addEventListener('click', (event) => {
    const closeBtn = event.target.closest('[data-file-tab-close]');
    if (closeBtn) { removeFileTab(closeBtn.dataset.fileTabClose); return; }
    const tabEl = event.target.closest('[data-file-tab]');
    if (tabEl) activateFileTab(tabEl.dataset.fileTab);
  });
  $('#filePanelBody').addEventListener('click', async (event) => {
    if (event.target.closest('[data-file-edit]')) { startFileEdit(filePanelState.activeKey); return; }
    if (event.target.closest('[data-file-edit-cancel]')) { cancelFileEdit(filePanelState.activeKey); return; }
    if (event.target.closest('[data-file-save]')) { saveFileTab(filePanelState.activeKey); return; }
    const codeButton = event.target.closest('[data-copy-code]');
    if (codeButton) {
      const code = codeButton.closest('.code-block')?.querySelector('code');
      if (!code) return;
      try {
        await copyText(code.textContent);
        codeButton.textContent = '已复制';
        clearTimeout(codeButton.copyResetTimer);
        codeButton.copyResetTimer = setTimeout(() => {
          if (codeButton.isConnected) codeButton.textContent = '复制';
        }, 1500);
        toast('已复制代码');
      } catch (error) {
        toast(`复制失败：${error.message}`);
      }
      return;
    }
    const previewImage = event.target.closest('.file-image-wrap img[data-large-url]');
    if (previewImage) openImageLightbox(previewImage.dataset.largeUrl, previewImage);
  });
  const closeFilePanelButton = $('#closeFilePanel');
  if (closeFilePanelButton) closeFilePanelButton.addEventListener('click', () => closeFilePanel());
  const collapseSidebarButton = $('#collapseSidebar');
  if (collapseSidebarButton) collapseSidebarButton.addEventListener('click', () => setLeftSidebarCollapsed(true));
  const expandSidebarButton = $('#expandSidebar');
  if (expandSidebarButton) expandSidebarButton.addEventListener('click', () => setLeftSidebarCollapsed(false));
  const openFileTabsButton = $('#openFileTabs');
  if (openFileTabsButton) openFileTabsButton.addEventListener('click', reopenFilePanel);
  window.addEventListener('resize', () => {
    // 文件面板在两种形态下都可用（桌面右栏 / 手机全屏抽屉），跨越 760px 时只需重算列宽，
    // 不再像以前那样把面板关掉——那正是手机端"没有打开文件能力"的来源。
    if (filePanelState.open) applyFilePanelOpenClass();
    if (!sidebarDesktop()) $('#appShell')?.classList.remove('sidebar-collapsed');
    updateFileTabsButton();
  });
  document.addEventListener('keydown', (event) => {
    // 大图灯箱打开时优先消费 ←/→/Esc（避免同时触发文件面板的 Esc 收尾）。
    if (handleImageLightboxKey(event)) return;
    if (event.key !== 'Escape' || !filePanelState.open || !filePanelUsable()) return;
    if (document.querySelector('dialog[open]')) return;
    const tab = activeFileTab();
    if (tab && tab.editing) { event.preventDefault(); cancelFileEdit(tab.key); return; }
    closeFilePanel();
  });
  // 侧栏宽度可调：拖动 resizer，限制在 [170, 窗口30%]，并持久化
  const sidebarResizer = $('#sidebarResizer');
  if (sidebarResizer) {
    const clampSidebarW = (w) => Math.max(170, Math.min(Math.max(170, window.innerWidth * 0.3), w));
    sidebarResizer.addEventListener('mousedown', (event) => {
      event.preventDefault();
      sidebarResizer.classList.add('dragging');
      const shellLeft = (document.querySelector('.app-shell')?.getBoundingClientRect().left || 0);
      const onMove = (e) => {
        document.documentElement.style.setProperty('--sidebar-w', clampSidebarW(e.clientX - shellLeft) + 'px');
      };
      const onUp = () => {
        sidebarResizer.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        const w = document.documentElement.style.getPropertyValue('--sidebar-w');
        localStorage.setItem('naibaChatSidebarW', w);
        renderSidebar();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }
  const filePanelResizer = $('#filePanelResizer');
  if (filePanelResizer) filePanelResizer.addEventListener('mousedown', (event) => {
    if (!filePanelState.open) return; event.preventDefault(); filePanelResizer.classList.add('dragging');
    const move = (e) => { const w = Math.max(280, Math.min(Math.floor(window.innerWidth * 0.5), window.innerWidth - e.clientX)); $('#appShell')?.style.setProperty('--file-panel-w', `${w}px`); };
    const up = () => { filePanelResizer.classList.remove('dragging'); document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); const w = parseFloat(getComputedStyle($('#appShell')).getPropertyValue('--file-panel-w')); if (Number.isFinite(w)) localStorage.setItem('naibaChatFilePanelW', String(Math.round(w))); };
    document.addEventListener('mousemove', move); document.addEventListener('mouseup', up);
  });
  // 窗口大小变化时，把侧栏宽度压回窗口 30% 上限，并重绘虚拟列表
  const recalcSidebarW = () => {
    const cur = parseFloat(document.documentElement.style.getPropertyValue('--sidebar-w') || 0) || 272;
    const maxW = Math.max(170, window.innerWidth * 0.3);
    if (cur > maxW) {
      document.documentElement.style.setProperty('--sidebar-w', maxW + 'px');
      localStorage.setItem('naibaChatSidebarW', maxW + 'px');
    }
    renderSidebar();
    if (filePanelState.open) applyFilePanelOpenClass();
  };
  window.addEventListener('resize', recalcSidebarW);
  $('#addWorkspace').addEventListener('click', createWorkspace);
  $('#workspaceSort').addEventListener('click', () => {
    state.workspaceSort = state.workspaceSort === 'updated' ? 'name' : 'updated';
    renderSidebar();
    toast(state.workspaceSort === 'name' ? '已按名称排序' : '已按时间排序');
  });
  const searchRow = $('#workspaceSearchRow');
  $('#workspaceSearch').addEventListener('click', () => {
    const input = $('#workspaceSearchInput');
    if (!searchRow || !input) return;
    searchRow.hidden = !searchRow.hidden;
    if (!searchRow.hidden) {
      syncSearchModeUi();
      input.focus();
    } else {
      // 关闭搜索：两种模式都要复位，否则树区域会停在「全文结果」上。
      input.value = '';
      state.workspaceSearch = '';
      state.searchResults = null;
      state.searchQuery = '';
      setWorkspaceSearchMode('title');
      renderSidebar();
    }
  });
  // 「标题｜全文」切换：默认标题（本地过滤，行为与升级前一致）；全文模式且有词时顺带检索一次。
  $('#workspaceSearchModeTitle')?.addEventListener('click', () => setWorkspaceSearchMode('title'));
  $('#workspaceSearchModeFull')?.addEventListener('click', () => setWorkspaceSearchMode('full'));
  // 输入：标题模式即时本地过滤；全文模式防抖 300ms 再打接口（逐字符打库没有必要）。
  let searchDebounceTimer = 0;
  $('#workspaceSearchInput').addEventListener('input', (event) => {
    state.workspaceSearch = event.target.value;
    if (state.workspaceSearchMode !== 'full') {
      renderSidebar();
      return;
    }
    window.clearTimeout(searchDebounceTimer);
    searchDebounceTimer = window.setTimeout(() => { void runFullTextSearch(); }, SEARCH_DEBOUNCE_MS);
  });
  // Esc：清空关键词退回会话树——结果列表是**替换**会话树渲染的，必须有键盘出口。
  $('#workspaceSearchInput').addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    event.target.value = '';
    state.workspaceSearch = '';
    state.searchResults = null;
    state.searchQuery = '';
    window.clearTimeout(searchDebounceTimer);
    renderSidebar();
  });
  $('#composerWorkspaceSelect').addEventListener('change', onComposerWorkspaceChange);
  $('#saveWorkspace').addEventListener('click', saveWorkspaceSettings);
  $('#browseWorkspace')?.addEventListener('click', () => pickWorkspace('workspaceDialogInput'));
  $('#browseWorkspaceSettings')?.addEventListener('click', () => pickWorkspace('workspaceDir'));
  $('#workspaceRefresh')?.addEventListener('click', () => loadWorkspaceTree(state.workspaceBrowsePath || ''));
  $('#workspaceUp')?.addEventListener('click', () => {
    const current = String(state.workspaceBrowsePath || '');
    const parent = current.replace(/[\\/][^\\/]+[\\/]?$/, '');
    if (parent && parent !== current) loadWorkspaceTree(parent);
  });
  $$('.settings-nav button').forEach((button) => button.addEventListener('click', () => switchSettingsTab(button.dataset.settingsTab)));
  $('#settingsSearch')?.addEventListener('input', (event) => {
    const query = String(event.target.value || '').trim().toLowerCase();
    let visible = 0;
    $$('.settings-nav button[data-settings-tab]').forEach((button) => {
      const haystack = `${button.textContent} ${button.dataset.settingsSearch || ''}`.toLowerCase();
      const match = !query || haystack.includes(query);
      button.hidden = !match;
      if (match) visible += 1;
    });
    $$('.settings-nav-group').forEach((group) => { group.hidden = !group.querySelector('button[data-settings-tab]:not([hidden])'); });
    const empty = $('#settingsNavEmpty'); if (empty) empty.hidden = visible > 0;
  });
  // API 供应商卡片：整张卡可点即打开设置弹层；右上角 × 删除；末尾「添加 API」卡片新建。
  $('#providerCards').addEventListener('click', (event) => {
    const remove = event.target.closest('[data-provider-delete]');
    if (remove) {
      deleteProvider(remove.dataset.providerDelete).catch((error) => toast(`删除失败：${error.message}`));
      return;
    }
    if (event.target.closest('[data-provider-add]')) { addProvider(); return; }
    const card = event.target.closest('[data-provider-card]');
    if (card) openProviderCard(card.dataset.providerCard);
  });
  $('#providerCards').addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const card = event.target.closest('[data-provider-card]');
    if (!card) return;
    event.preventDefault();
    openProviderCard(card.dataset.providerCard);
  });
  // Esc / 右上角关闭 / 取消：统一由 close 事件复位编辑态并重绘卡片。
  $('#providerDialog').addEventListener('close', () => cancelProviderEdit());
  $$('[data-provider-kind]').forEach((button) => button.addEventListener('click', () => {
    if (state.providerEditing || state.providerKindTab === button.dataset.providerKind) return;
    state.providerKindTab = button.dataset.providerKind;
    renderProviders();
  }));
  $('#providerForm').addEventListener('submit', saveProvider);
  $('#cancelProvider').addEventListener('click', cancelProviderEdit);
  $('#testProvider').addEventListener('click', testProvider);
  $('#loadProviderModels').addEventListener('click', () => loadProviderModels());
  $('#unloadProviderModel').addEventListener('click', unloadConfiguredProviderModel);
  $('#providerFormat').addEventListener('change', () => {
    syncProviderKindOptions();
    updateProviderFormatGuide();
    updateProviderContextField();
  });
  $('#providerModel').addEventListener('change', () => {
    toggleCustomModel();
    applyProviderModelCapabilities();
  });
  $('#providerSupportsImages').addEventListener('change', updateProviderVisionHint);
  $('#toggleProviderKey').addEventListener('click', toggleProviderKey);
  $('#providerApiKey').addEventListener('input', (event) => {
    if (event.target.value) $('#providerKeyStatus').textContent = '待保存';
  });
  // 供应商模板网格：点一张卡即回填名称/地址/请求格式/推荐模型 + 切引导文案（事件委托，
  // 卡片是渲染出来的，逐张绑会在重渲染后丢）。
  $('#providerPresetGrid').addEventListener('click', (event) => {
    const card = event.target.closest('[data-provider-preset]');
    if (card) applyProviderPreset(card.dataset.providerPreset);
  });
  // 「打开注册页」：地址由服务端按预设白名单取，前端只递 preset_id（见 09-settings.js）。
  $('#providerPresetKeyUrl').addEventListener('click', (event) => {
    openProviderPresetKeyUrl(event.currentTarget.dataset.presetId).catch((error) => toast(`打开注册页失败：${error.message}`));
  });
  // 首启引导：选模板 → 填 Key → 测试/保存；「重新选择」回第 1 步；任何关闭路径都记「不再打扰」。
  $('#onboardingPresetGrid').addEventListener('click', (event) => {
    const card = event.target.closest('[data-provider-preset]');
    if (card) selectOnboardingPreset(card.dataset.providerPreset);
  });
  $('#onboardingKeyUrl').addEventListener('click', (event) => {
    openProviderPresetKeyUrl(event.currentTarget.dataset.presetId).catch((error) => toast(`打开注册页失败：${error.message}`));
  });
  $('#onboardingBack').addEventListener('click', resetOnboarding);
  $('#onboardingSkip').addEventListener('click', () => $('#onboardingDialog').close());
  $('#onboardingTest').addEventListener('click', () => { testOnboardingConnection(); });
  $('#onboardingSave').addEventListener('click', () => { saveOnboardingProvider(); });
  $('#onboardingDialog').addEventListener('close', dismissOnboarding);
  // Agent 卡片：整张卡可点即打开设置弹层；右上角 × 删除；末尾「新增 Agent」卡片新建。
  $('#agentCards').addEventListener('click', (event) => {
    const remove = event.target.closest('[data-agent-delete]');
    if (remove) {
      deleteAgent(remove.dataset.agentDelete).catch((error) => toast(`删除失败：${error.message}`));
      return;
    }
    if (event.target.closest('[data-agent-add]')) { showAgentForm(null); return; }
    const card = event.target.closest('[data-agent-card]');
    if (card) openAgentCard(card.dataset.agentCard);
  });
  $('#agentCards').addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const card = event.target.closest('[data-agent-card]');
    if (!card) return;
    event.preventDefault();
    openAgentCard(card.dataset.agentCard);
  });
  // Esc / 右上角关闭 / 取消：统一由 close 事件复位（与供应商弹层同一套路）。
  $('#agentDialog').addEventListener('close', () => hideAgentForm());
  // 分区切换：顶端按钮（基本 / 系统提示词 / 固定 Skill / 工具集）
  $$('.agent-tabs button[data-agent-tab]').forEach((button) => {
    button.addEventListener('click', () => switchAgentTab(button.dataset.agentTab));
  });
  $('#agentSkillList').addEventListener('change', (event) => {
    if (event.target.type !== 'checkbox') return;
    state.agentFormSkillIds = event.target.checked
      ? [...new Set([...state.agentFormSkillIds, event.target.value])]
      : state.agentFormSkillIds.filter((id) => id !== event.target.value);
    updateAgentSkillTabCount();
  });
  $('#cancelAgent').addEventListener('click', hideAgentForm);
  $('#saveAgentForm').addEventListener('click', saveAgentForm);
  $('#toggleAllToolGroups')?.addEventListener('click', toggleAllToolGroups);
  // 工具集搜索框：输入即重画列表（分组视图 ↔ 平铺搜索结果），不重拉目录、不动已选范围。
  $('#agentToolFilter')?.addEventListener('input', (event) => {
    state.agentToolFilter = event.target.value;
    renderToolScopeList();
  });
  // 工具集：卡片态（预设/我的工具集/添加卡，事件委托 + Enter/Space 等同点击）
  $('#agentToolPresetCards')?.addEventListener('click', handleAgentToolPresetCardsClick);
  $('#agentToolPresetCards')?.addEventListener('keydown', handleAgentToolPresetCardsKeydown);
  // 编辑态：横条上的返回 / 保存（名称输入框回车 = 保存）
  $('#agentToolEditorBack')?.addEventListener('click', closeAgentToolEditor);
  $('#agentToolEditorSave')?.addEventListener('click', saveAgentToolSet);
  $('#agentToolSetName')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      saveAgentToolSet();
    }
  });
  $('#saveRuntime').addEventListener('click', saveRuntimeSettings);
  document.querySelectorAll('input[name="proxyMode"]').forEach((radio) => {
    radio.addEventListener('change', renderProxyRows);
  });
  $('#cleanImageCache')?.addEventListener('click', cleanImageCache);
  $('#refreshStorageStats')?.addEventListener('click', loadStorageStats);
  $('#compactDatabase')?.addEventListener('click', compactDatabase);
  $('#imageUploadOriginal')?.addEventListener('change', renderImageCompressRow);
  $('#imageLightboxClose')?.addEventListener('click', closeImageLightbox);
  $('#imageLightboxPrev')?.addEventListener('click', (event) => { event.stopPropagation(); stepImageLightbox(-1); });
  $('#imageLightboxNext')?.addEventListener('click', (event) => { event.stopPropagation(); stepImageLightbox(1); });
  // 灯箱的点击翻页/缩放/拖动/触屏手势统一在 03-media 内初始化（状态就近管理）。
  initImageLightboxInteractions();
  initTurnRail();
  $('#saveToken').addEventListener('click', saveAccessToken);
  $('#checkUpdate').addEventListener('click', checkUpdate);
  $('#installUpdate').addEventListener('click', installUpdate);
  $('#updateVersionSelect').addEventListener('change', () => renderUpdateStatus(state.bootstrap.update || {}));
  $('#openSkillImport').addEventListener('click', () => {
    setSkillImportStatus('');
    $('#skillImportDialog').showModal();
  });
  $('#skillDeleteRecycle')?.addEventListener('click', () => runInstalledSkillDelete('recycle'));
  $('#skillDeletePermanent')?.addEventListener('click', () => runInstalledSkillDelete('permanent'));
  $('#clearSkillRecycle')?.addEventListener('click', clearSkillRecycle);
  $('#skillImportFolder').addEventListener('click', () => $('#skillImportFolderInput').click());
  $('#skillImportFiles').addEventListener('click', () => $('#skillImportFileInput').click());
  $('#skillImportFolderInput').addEventListener('change', (event) => { skillImportFolderFiles(event.target.files); event.target.value = ''; });
  $('#skillImportFileInput').addEventListener('change', (event) => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    const lower = file.name.toLowerCase();
    if (lower.endsWith('.zip')) skillImportZipFile(file);
    else if (lower.endsWith('.md')) skillImportMdFile(file);
    else setSkillImportStatus('仅支持 .zip 或 .md 文件', 'error');
  });
  const dropZone = $('#skillDropZone');
  if (dropZone) {
    dropZone.addEventListener('dragover', (event) => { event.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', (event) => {
      event.preventDefault();
      dropZone.classList.remove('dragover');
      const dt = event.dataTransfer;
      if (!dt) return;
      const folderEntry = [...dt.items].find((it) => it.kind === 'file' && it.webkitGetAsEntry && it.webkitGetAsEntry() && it.webkitGetAsEntry().isDirectory);
      if (folderEntry && folderEntry.webkitGetAsEntry) {
        readDirectoryEntry(folderEntry.webkitGetAsEntry()).then((files) => skillImportFolderFiles(files));
        return;
      }
      const files = [...dt.files];
      const zip = files.find((f) => f.name.toLowerCase().endsWith('.zip'));
      const md = files.find((f) => f.name.toLowerCase().endsWith('.md'));
      if (files.length === 1 && (zip || md)) {
        if (zip) skillImportZipFile(zip); else skillImportMdFile(md);
      } else if (files.length) {
        skillImportFolderFiles(files);
      }
    });
  }
  $('#backupData').addEventListener('click', backupData);
  $('#applyDataDir').addEventListener('click', applyAndMigrateDataDir);
}

export function setSkillImportStatus(message, kind) {
  const el = $('#skillImportStatus');
  if (!el) return;
  el.textContent = message;
  el.className = 'skill-import-status' + (kind ? ' ' + kind : '');
}

export function hasSkillFrontmatter(text) {
  const m = text.match(/^---\s*\n([\s\S]*?)\n---/);
  if (!m) return false;
  const block = m[1];
  return /^\s*name\s*:/im.test(block) && /^\s*description\s*:/im.test(block);
}

export async function skillImportFolderFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  if (files.length > 2000) { setSkillImportStatus('文件夹内文件数量过多（超过 2000）', 'error'); return; }
  const totalSize = files.reduce((sum, f) => sum + f.size, 0);
  if (totalSize > 80 * 1024 * 1024) { setSkillImportStatus('文件夹总大小不能超过 80 MB', 'error'); return; }
  setSkillImportStatus(`正在上传 ${files.length} 个文件…`);
  try {
    const payload = [];
    for (const file of files) {
      const data = await readAsDataUrl(file);
      payload.push({ path: file._relPath || file.webkitRelativePath || file.name, data });
    }
    const result = await api('/api/skills/install_folder', { method: 'POST', body: { files: payload } });
    if (result.configured) state.skillDirs = result.configured;
    renderInstalledSkills(result.skills || []);
    renderHiddenSkills(result.hidden_skills || []);
    renderRecycledSkills(result.recycled_skills || []);
    const unhiddenTag = (result.unhidden && result.unhidden.length) ? ` · 已安装并取消隐藏` : '';
    setSkillImportStatus(`已安装 ${result.files} 个文件到 ${result.dir}${unhiddenTag}`, 'ok');
  } catch (error) {
    setSkillImportStatus(`安装失败：${error.message}`, 'error');
  }
}

export async function skillImportZipFile(file) {
  setSkillImportStatus(`正在上传 ${file.name}…`);
  try {
    const data = await readAsDataUrl(file);
    const result = await api('/api/skills/install', { method: 'POST', body: { name: file.name, data } });
    if (result.configured) state.skillDirs = result.configured;
    renderInstalledSkills(result.skills || []);
    renderHiddenSkills(result.hidden_skills || []);
    renderRecycledSkills(result.recycled_skills || []);
    const unhiddenTag = (result.unhidden && result.unhidden.length) ? ' · 已安装并取消隐藏' : '';
    setSkillImportStatus(`已安装到 ${result.dir}${unhiddenTag}`, 'ok');
  } catch (error) {
    setSkillImportStatus(`安装失败：${error.message}`, 'error');
  }
}

export async function skillImportMdFile(file) {
  setSkillImportStatus(`正在读取 ${file.name}…`);
  try {
    const data = await readAsDataUrl(file);
    const result = await api('/api/skills/install_folder', { method: 'POST', body: { files: [{ path: 'SKILL.md', data }] } });
    if (result.configured) state.skillDirs = result.configured;
    renderInstalledSkills(result.skills || []);
    renderHiddenSkills(result.hidden_skills || []);
    renderRecycledSkills(result.recycled_skills || []);
    const unhiddenTag = (result.unhidden && result.unhidden.length) ? ' · 已安装并取消隐藏' : '';
    setSkillImportStatus(`已作为 Skill 安装到 ${result.dir}${unhiddenTag}`, 'ok');
  } catch (error) {
    setSkillImportStatus(`安装失败：${error.message}`, 'error');
  }
}

export function readDirectoryEntry(entry) {
  const files = [];
  const walk = (ent, prefix) => new Promise((res) => {
    if (ent.isFile) {
      ent.file((file) => { file._relPath = prefix + file.name; files.push(file); res(); });
    } else if (ent.isDirectory) {
      const reader = ent.createReader();
      const readBatch = () => reader.readEntries((batch) => {
        if (!batch.length) { res(); return; }
        Promise.all(batch.map((b) => walk(b, prefix + ent.name + '/'))).then(readBatch);
      });
      readBatch();
    } else {
      res();
    }
  });
  return walk(entry, '').then(() => files);
}

export async function loadInstalledSkills(showToast) {
  try {
    const data = await api('/api/skills/scan', { method: 'POST', body: {} });
    if (data.configured) state.skillDirs = data.configured;
    renderInstalledSkills(data.skills || []);
    renderHiddenSkills(data.hidden_skills || []);
    renderRecycledSkills(data.recycled_skills || []);
    if (showToast) toast('已重新扫描 Skill');
  } catch (error) {
    toast(`扫描失败：${error.message}`);
  }
}

export let lastInstalledSkills = [];
let pendingSkillDelete = null;

export function isSkillBuiltin(skill) {
  return skill.source === 'builtin';
}

export function renderInstalledSkills(skills) {
  lastInstalledSkills = skills || [];
  if (state.bootstrap) {
    state.bootstrap.skills = lastInstalledSkills;
    const available = new Set(lastInstalledSkills.map((skill) => skill.id));
    state.selectedSkills = state.selectedSkills.filter((id) => available.has(id));
    localStorage.setItem('naibaChatSkillIds', JSON.stringify(state.selectedSkills));
    renderSkills($('#skillSearch')?.value || '');
    renderAgentSkillPicker();
  }
  const list = $('#installedSkillList');
  list.innerHTML = '';
  $('#installedSkillCount').textContent = String(skills.length);
  if (!skills.length) {
    list.innerHTML = '<div class="connection-item"><small>未加载任何 Skill</small></div>';
    return;
  }
  skills.forEach((skill) => {
    const item = document.createElement('div');
    item.className = 'skill-item connection-item';
    const info = document.createElement('div');
    const b = document.createElement('b');
    b.textContent = skill.name;
    const small = document.createElement('small');
    small.textContent = skill.description || skill.path;
    small.className = 'desc';
    info.append(b, small);
    item.append(info);
    if (skill.source === 'builtin' || skill.source === 'external') {
      const tag = document.createElement('span');
      tag.className = 'skill-tag';
      tag.textContent = skill.source === 'builtin' ? '内置' : '外部';
      item.append(tag);
    }
    const dupDirs = Array.isArray(skill.duplicate_dirs) ? skill.duplicate_dirs : [];
    // 内置 Skill 必有「打包一份 + 托管副本」两份，重复徽标只提示非内置的多目录重复。
    if (dupDirs.length > 1 && skill.source !== 'builtin') {
      const dup = document.createElement('span');
      dup.className = 'skill-tag';
      dup.textContent = `重复 ${dupDirs.length} 处`;
      dup.title = `同一 Skill 在以下目录各有一份：\n${dupDirs.join('\n')}`;
      item.append(dup);
    }
    const del = document.createElement('button');
    del.className = 'skill-delete';
    del.type = 'button';
    del.title = skill.source === 'managed' && dupDirs.length <= 1
      ? '删除 Skill（移动到回收目录）'
      : '隐藏 Skill（可在「已隐藏」中恢复；多处副本一并隐藏）';
    del.textContent = '删除';
    del.addEventListener('click', () => openInstalledSkillDelete(skill));
    item.append(del);
    list.append(item);
  });
}

export function openInstalledSkillDelete(skill) {
  const refs = (state.bootstrap.agents || [])
    .filter((a) => (a.skill_ids || []).map(String).includes(String(skill.id)))
    .map((a) => a.name);
  const dir = skill.path || skill.root || '';
  const dupDirs = Array.isArray(skill.duplicate_dirs) ? skill.duplicate_dirs : [];
  const bundled = skill.source === 'builtin';
  const shared = skill.source !== 'managed' || dupDirs.length > 1;
  const canDeleteFiles = skill.source === 'managed' && !shared;
  const dialog = $('#skillDeleteDialog');
  if (!dialog) return;
  pendingSkillDelete = skill;
  $('#skillDeleteDialogSummary').textContent = `Skill：${skill.name}`;
  const refText = refs.length ? `被以下 Agent 引用：${refs.join('、')}。删除后会移除引用。` : '未被任何 Agent 引用。';
  let details = `位置：${dir}\n${refText}`;
  if (canDeleteFiles) {
    details += '\n可将整个托管目录移入回收目录，或直接永久删除。';
  } else if (bundled) {
    details += '\n这是程序内置 Skill。操作会按 ID 隐藏它；托管目录中的副本会一并处理，程序包原件不可删除。';
  } else {
    details += `\n该 Skill ${dupDirs.length > 1 ? '在多个目录中存在副本' : '来自外部目录'}，为了避免删除用户文件，只能隐藏全部同 ID 副本。`;
  }
  $('#skillDeleteDialogDetails').textContent = details;
  const recycle = $('#skillDeleteRecycle');
  const permanent = $('#skillDeletePermanent');
  recycle.textContent = canDeleteFiles ? '删除（移到回收目录，可恢复）' : '隐藏（可恢复）';
  permanent.hidden = !canDeleteFiles && !bundled;
  if (bundled) permanent.textContent = '彻底删除托管副本';
  else permanent.textContent = '彻底删除（直接删文件，不可恢复）';
  dialog.showModal();
}

export async function runInstalledSkillDelete(mode) {
  const skill = pendingSkillDelete;
  if (!skill) return;
  const dialog = $('#skillDeleteDialog');
  const recycle = $('#skillDeleteRecycle');
  const permanent = $('#skillDeletePermanent');
  if (mode === 'permanent' && !confirm(`彻底删除 Skill「${skill.name}」的托管文件？此操作不可恢复。`)) return;
  if (dialog?.open) dialog.close();
  if (recycle) recycle.disabled = true;
  if (permanent) permanent.disabled = true;
  try {
    const result = await api('/api/skills/delete', { method: 'POST', body: { skill_id: skill.id, mode } });
    state.selectedSkills = state.selectedSkills.filter((id) => id !== skill.id);
    localStorage.setItem('naibaChatSkillIds', JSON.stringify(state.selectedSkills));
    if (result.skills) state.bootstrap.skills = result.skills;
    if (result.agents) state.bootstrap.agents = result.agents;
    renderInstalledSkills(result.skills || lastInstalledSkills.filter((s) => s.id !== skill.id));
    renderSkills($('#skillSearch')?.value || '');
    renderHiddenSkills(result.hidden_skills || []);
    renderRecycledSkills(result.recycled_skills || []);
    if (result.hidden) {
      toast(result.permanently_deleted ? '已隐藏，托管副本已彻底删除' : (result.managed_copy_removed ? '已隐藏，托管副本已移到回收目录' : '已隐藏，可在「已隐藏 Skill」中恢复'));
    } else if (result.permanently_deleted) {
      toast('已彻底删除');
    } else {
      toast(`已移到回收目录：${result.recycled_to || '未知'}`);
    }
  } catch (error) {
    toast(`删除失败：${error.message}`);
  } finally {
    pendingSkillDelete = null;
    if (recycle) recycle.disabled = false;
    if (permanent) permanent.disabled = false;
  }
}

export async function clearSkillRecycle() {
  if (!confirm('清空 Skill 回收目录？其中的文件将永久删除，且无法恢复。')) return;
  try {
    const result = await api('/api/skills/recycle/clear', { method: 'POST', body: {} });
    renderRecycledSkills([]);
    toast(result.deleted ? `已永久删除回收目录中的 ${result.deleted} 项` : '回收目录已是空的');
  } catch (error) {
    toast(`清空回收目录失败：${error.message}`);
  }
}

export function renderRecycledSkills(recycledSkills) {
  const list = $('#recycledSkillList');
  if (!list) return;
  const items = Array.isArray(recycledSkills) ? recycledSkills : [];
  const count = $('#recycledSkillCount');
  if (count) count.textContent = String(items.length);
  list.innerHTML = '';
  if (!items.length) {
    list.innerHTML = '<small class="hint">回收目录为空</small>';
    return;
  }
  items.forEach((skill) => {
    const item = document.createElement('div');
    item.className = 'skill-item connection-item';
    const info = document.createElement('div');
    const name = document.createElement('b');
    name.textContent = skill.name;
    const path = document.createElement('small');
    path.className = 'desc';
    path.textContent = skill.path || '';
    info.append(name, path);
    const restore = document.createElement('button');
    restore.className = 'skill-delete';
    restore.type = 'button';
    restore.textContent = '恢复';
    restore.title = '恢复到托管 Skill 目录并重新启用';
    restore.addEventListener('click', () => restoreRecycledSkill(skill));
    item.append(info, restore);
    list.append(item);
  });
}

export async function restoreRecycledSkill(skill) {
  if (!skill?.entry) return;
  try {
    const result = await api('/api/skills/recycle/restore', { method: 'POST', body: { entry: skill.entry } });
    if (state.bootstrap && result.skills) state.bootstrap.skills = result.skills;
    renderInstalledSkills(result.skills || []);
    renderHiddenSkills(result.hidden_skills || []);
    renderRecycledSkills(result.recycled_skills || []);
    renderSkills($('#skillSearch')?.value || '');
    toast('Skill 已恢复');
  } catch (error) {
    toast(`恢复失败：${error.message}`);
  }
}

export function renderHiddenSkills(hiddenSkills) {
  const list = $('#hiddenSkillList');
  if (!list) return;
  const items = Array.isArray(hiddenSkills) ? hiddenSkills : [];
  const count = $('#hiddenSkillCount');
  if (count) count.textContent = String(items.length);
  list.innerHTML = '';
  if (!items.length) {
    list.innerHTML = '<small class="hint">无已隐藏的 Skill</small>';
    return;
  }
  items.forEach((skill) => {
    const item = document.createElement('div');
    item.className = 'skill-item connection-item';
    const info = document.createElement('div');
    const b = document.createElement('b');
    b.textContent = skill.name;
    const small = document.createElement('small');
    small.textContent = skill.description || skill.path;
    small.className = 'desc';
    info.append(b, small);
    item.append(info);
    const unhide = document.createElement('button');
    unhide.className = 'skill-delete';
    unhide.type = 'button';
    unhide.title = '取消隐藏，恢复为可用的 Skill';
    unhide.textContent = '取消隐藏';
    unhide.addEventListener('click', () => unhideSkill(skill.id));
    item.append(unhide);
    list.append(item);
  });
}

export async function unhideSkill(skillId) {
  if (!skillId) return;
  try {
    const result = await api('/api/skills/unhide', { method: 'POST', body: { skill_id: skillId } });
    if (state.bootstrap && result.skills) state.bootstrap.skills = result.skills;
    renderInstalledSkills(result.skills || []);
    renderHiddenSkills(result.hidden_skills || []);
    renderSkills($('#skillSearch')?.value || '');
    toast('已取消隐藏该 Skill');
  } catch (error) {
    toast(`取消隐藏失败：${error.message}`);
  }
}

export function renderDataMigration() {
  const m = state.bootstrap?.data_migration || {};
  const configured = m.configured_data_dir || state.bootstrap?.settings?.resolved_data_dir || m.data_dir || '';
  if ($('#dataDir') && document.activeElement !== $('#dataDir')) $('#dataDir').value = configured;
  $('#migrationDbVersion').textContent = m.db_version != null ? String(m.db_version) : '-';
  $('#migrationDataDir').textContent = m.restart_required
    ? `${m.data_dir || '-'}（重启后切换到 ${configured}）`
    : (configured || m.data_dir || '-');
  const skillsDirs = Array.isArray(m.resolved_skills_dirs) ? m.resolved_skills_dirs : [];
  $('#migrationSkillsDir').textContent = skillsDirs.length ? skillsDirs.join('；') : '-';
  $('#migrationHealthy').textContent = m.healthy === true ? '✓ 健康' : (m.healthy === false ? '✗ 异常' : '-');
  $('#migrationApplied').textContent = Array.isArray(m.applied_versions)
    ? (m.applied_versions.length ? m.applied_versions.join(', ') : '无')
    : '-';
  $('#migrationBackup').textContent = m.backup_location || '-';
}

export async function applyAndMigrateDataDir() {
  const value = $('#dataDir')?.value.trim() || '';
  if (!value) {
    $('#migrationMessage').textContent = '请先填写目标数据目录';
    return;
  }
  if (!confirm('将当前数据库、上传文件与 Skills 目录一并复制到新目录，并完成结构迁移。完成后需要重启，继续？')) return;
  const btn = $('#applyDataDir');
  const previousText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '迁移中…'; }
  try {
    const result = await api('/api/migration/move-data', { method: 'POST', body: { data_dir: value } });
    if (!result.ok) {
      $('#migrationMessage').textContent = `迁移失败：${result.error || '未知错误'}`;
      return;
    }
    const target = result.target_data_dir || value;
    const skills = result.target_skills_dir || (result.resolved_skills_dirs && result.resolved_skills_dirs[0]) || '';
    $('#migrationMessage').textContent = `数据库与 Skills 已复制到新目录（数据：${target}${skills ? `；Skills：${skills}` : ''}），请完全退出并重新启动 Cat Chat 生效。`;
    state.bootstrap.data_migration = result;
    renderDataMigration();
  } catch (error) {
    $('#migrationMessage').textContent = `迁移失败：${error.message}`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = previousText; }
  }
}

export async function loadDataMigrationHealth() {
  try {
    const m = await api('/api/migration/health');
    state.bootstrap.data_migration = m;
    renderDataMigration();
  } catch (error) {
    toast(`读取迁移状态失败：${error.message}`);
  }
}

export async function backupData() {
  try {
    const r = await api('/api/migration/backup', { method: 'POST', body: {} });
    if (r.error) { $('#migrationMessage').textContent = '备份失败：' + r.error; return; }
    const files = Array.isArray(r.files) ? `（${r.files.length} 个文件）` : '';
    $('#migrationMessage').textContent = `已备份到：${r.backup_dir || ''}${files}`;
    if (r.backup_location) { state.bootstrap.data_migration = state.bootstrap.data_migration || {}; state.bootstrap.data_migration.backup_location = r.backup_location; }
    renderDataMigration();
  } catch (error) {
    $('#migrationMessage').textContent = '备份失败：' + error.message;
  }
}

export function switchSettingsTab(name) {
  $$('.settings-nav button').forEach((button) => button.classList.toggle('active', button.dataset.settingsTab === name));
  $$('[data-settings-panel]').forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== name; });
  if (name === 'appearance') {
    const theme = state.appearance?.theme || 'system';
    const skin = state.appearance?.skin || 'violet';
    $$('input[name="appearanceTheme"]').forEach((el) => { el.checked = el.value === theme; });
    $$('input[name="appearanceSkin"]').forEach((el) => { el.checked = el.value === skin; });
  }
  if (name === 'agent') renderAgentManager();
  if (name === 'skills') loadInstalledSkills(false);
  if (name === 'connections') loadMcpServers();
  if (name === 'datamigration') loadDataMigrationHealth();
  if (name === 'updates') api('/api/update').then((status) => {
    state.bootstrap.update = status;
    renderUpdateStatus(status);
  }).catch((error) => toast(`读取更新状态失败：${error.message}`));
}

bindEvents();
initialize();
// 首屏输入框为空：发送按钮从加载起就是灰暗的不可发送态。
updateSendButtonState();
restoreLeftSidebarCollapse();
restoreTopbarCompact();
updateFileTabsButton();
