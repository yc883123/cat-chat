// ============================================================
// 09-settings.js —— 拆分自 public/app.js 第 3115-4672 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, $$, agentAvatarEmoji, agentAvatarSrc, api, applyAppearance, applyChatBackground, CHAT_FONT_PICKS, chatBackgroundCrop, chatBackgroundCropScale, chatBackgroundCropScaleLimits, chatBackgroundImageAspect, escapeHtml, isFontInstalled, localFileUrl, refreshChatBackgroundImageStatus, saveAppearance, state, toast } from "./01-core.js";
import { applyConversationAgent, populateComposerModels, populateModels, renderAgents, updateUnloadModelButton } from "./07-models-agents.js";
import { closeAgentPromptPresetPanel, currentAgentFixedSkillIds, renderAgentPromptPresetList } from "./08-conversations.js";
import { skillList } from "./13-skill-refs.js";
import { playDoneSound } from "./20-sound.js";
import { switchSettingsTab } from "./15-bind-events.js";

// 「指定字体」radio 的占位值：只活在 DOM 里，**永不落库**（见 appearanceFormValues）。
// 后端白名单不认它 ⇒ 若不翻译会直接保存失败（最典型的事故面）。
const CHAT_FONT_PICK_VALUE = '__pick__';
// 结构三档：除它们之外的所有字体键（含 custom）都走「指定字体」下拉表达。
const CHAT_FONT_BASE_FAMILIES = new Set(['system', 'serif', 'rounded']);

export function renderSkills(filter = '') {
  if (!state.bootstrap) return;
  const query = filter.trim().toLowerCase();
  const fixed = new Set(currentAgentFixedSkillIds());
  const skills = state.bootstrap.skills.filter((skill) =>
    !query || `${skill.name} ${skill.description} ${skill.ref || ''}`.toLowerCase().includes(query));
  $('#skillList').innerHTML = skills.map((skill) => `
    <button type="button" class="skill-item skill-click" data-skill-insert="${skill.id}">
      <span><b>${escapeHtml(skill.name)}</b>${fixed.has(skill.id) ? '<em>预设</em>' : ''}<p>${escapeHtml(skill.description || '')}</p></span>
      <span class="skill-tag" title="点击插入到输入框">/${escapeHtml(skill.ref || skill.name)}</span>
    </button>`).join('');
  updateSkillSummary();
}

export function populateAppearanceSettings() {
  const appearance = state.bootstrap?.settings?.appearance || {};
  // 会话字体三键可能是旧版服务端没给的（undefined）——applyAppearance 会沿用当前值，
  // 所以这里直接透传，不做 || 兜底（否则"连上旧实例"会把用户字体重置回默认）。
  applyAppearance({
    theme: appearance.theme || 'system',
    skin: appearance.skin || 'violet',
    chat_font_size: appearance.chat_font_size,
    chat_font_family: appearance.chat_font_family,
    chat_font_family_custom: appearance.chat_font_family_custom,
    // 三个新键直接透传（undefined 时 applyAppearance 沿用当前值，见其回落约定）。
    done_sound: appearance.done_sound,
    done_sound_volume: appearance.done_sound_volume,
    tray_done_toast: appearance.tray_done_toast,
  });
  // 选项先建好再回显：select 里没有对应 option 时 value 会被清空。
  buildChatFontPickOptions();
  syncAppearanceControls();
  populateChatBackgroundSettings();
}

// 滑杆的「已填充部分」：纯 CSS 无法按控件当前值画轨道（轨道不知道 value），
// 于是由 JS 把百分比写进 --fill，交给 CSS 的 linear-gradient 着色。
// 纯视觉：不落库、不发请求，值本身仍由原有的预览/保存路径负责。
export function syncRangeFill(input) {
  if (!input || input.type !== 'range') return;
  const min = Number(input.min || 0);
  const max = Number(input.max || 100);
  const value = Number(input.value || 0);
  const ratio = max > min ? (value - min) / (max - min) : 0;
  input.style.setProperty('--fill', `${Math.round(Math.min(Math.max(ratio, 0), 1) * 100)}%`);
}

// 把 state.appearance 回显到外观面板的所有控件（主题 / 皮肤 / 会话字号 / 字体族）。
// 设置弹窗打开（populateAppearanceSettings）与保存/恢复默认之后都要调它，
// 否则"保存成功了但控件还显示旧值"。
export function syncAppearanceControls() {
  const appearance = state.appearance || {};
  const theme = appearance.theme || 'system';
  const skin = appearance.skin || 'violet';
  const size = Number(appearance.chat_font_size) || 15;
  const family = appearance.chat_font_family || 'system';
  const custom = appearance.chat_font_family_custom || '';
  $$('input[name="appearanceTheme"]').forEach((input) => { input.checked = input.value === theme; });
  $$('input[name="appearanceSkin"]').forEach((input) => { input.checked = input.value === skin; });
  // 结构三档各有自己的 radio；**任何预置字体键都落到「指定字体」那一项**（含 custom），
  // 由下面的 select 表达具体是谁——两层显隐分开：radio 管 select 行、select 管手动行。
  const usingPick = !CHAT_FONT_BASE_FAMILIES.has(family);
  const radioValue = usingPick ? CHAT_FONT_PICK_VALUE : family;
  $$('input[name="appearanceChatFont"]').forEach((input) => { input.checked = input.value === radioValue; });
  const slider = $('#chatFontSize');
  if (slider && slider.value !== String(size)) slider.value = String(size);
  syncRangeFill(slider);
  const output = $('#chatFontSizeValue');
  if (output) output.textContent = `${size}px`;
  const customRow = $('#chatFontCustomRow');
  if (customRow) customRow.hidden = !usingPick;
  const pick = $('#chatFontPick');
  if (pick && usingPick && pick.value !== family) pick.value = family;
  const manualRow = $('#chatFontManualRow');
  if (manualRow) manualRow.hidden = !(usingPick && family === 'custom');
  // 只在内容真的不同时才写回：文本框边打边预览会调用本函数，
  // 无脑 value= 会把光标甩到行尾（改中间字符时最难受）。
  const customInput = $('#chatFontFamilyCustom');
  if (customInput && customInput.value !== custom) customInput.value = custom;
  // 完成提示音 / 托盘完成卡片开关与音量回显（旧 index.html 没有这些节点时跳过）。
  const doneSound = $('#doneSoundToggle');
  if (doneSound) doneSound.checked = appearance.done_sound !== false;
  const volume = $('#doneSoundVolume');
  if (volume) {
    const vol = Number(appearance.done_sound_volume);
    const volValue = Number.isFinite(vol) ? Math.min(100, Math.max(0, Math.round(vol))) : 60;
    if (volume.value !== String(volValue)) volume.value = String(volValue);
    syncRangeFill(volume);
    const output = $('#doneSoundVolumeValue');
    if (output) output.textContent = `${volValue}%`;
  }
  const trayToast = $('#trayDoneToastToggle');
  if (trayToast) trayToast.checked = appearance.tray_done_toast !== false;
}

// 「指定字体」下拉的选项：依 CHAT_FONT_PICKS 现建（字体清单不进 HTML），
// 用 canvas 探测给没装的字体加「（未安装）」后缀——**只标注、不禁选**：
// 设置存在服务端、跨设备共享，这台没装不代表手机没装。
export function buildChatFontPickOptions() {
  const pick = $('#chatFontPick');
  if (!pick) return;
  const previous = pick.value;
  pick.textContent = '';
  CHAT_FONT_PICKS.forEach((item) => {
    const option = document.createElement('option');
    option.value = item.key;
    option.textContent = isFontInstalled(item.probe) ? item.label : `${item.label}（未安装）`;
    pick.appendChild(option);
  });
  if (previous) pick.value = previous;
}

// 面板控件的当前值 → 可直接交给 applyAppearance / saveAppearance 的对象。
// 控件在旧 index.html 里不存在时返回 undefined（= 这次没传，沿用当前值），
// 而不是默认值——否则打开一次设置就会把用户已存的偏好抹平。
export function appearanceFormValues() {
  const slider = $('#chatFontSize');
  const family = $('input[name="appearanceChatFont"]:checked');
  const customInput = $('#chatFontFamilyCustom');
  const pick = $('#chatFontPick');
  // __pick__ 只是前端占位值，**永远不许落库**（后端白名单会拒、表现为"保存失败"）：
  // 选中它时必须翻译成 select 里的真实键（预置键或 custom）。
  let fontFamily;
  if (family) {
    fontFamily = family.value === CHAT_FONT_PICK_VALUE
      ? (pick ? pick.value : undefined)
      : family.value;
  }
  return {
    theme: $('input[name="appearanceTheme"]:checked')?.value || 'system',
    skin: $('input[name="appearanceSkin"]:checked')?.value || 'violet',
    chat_font_size: slider ? Number(slider.value) : undefined,
    chat_font_family: fontFamily || undefined,
    // 只有「手动填写」时才带上用户串：选了预置字体就不去动它，
    // 用户上次打过的串因此原样留在库里，切回来还在。
    chat_font_family_custom: fontFamily === 'custom' && customInput ? customInput.value : undefined,
  };
}

// ---- 聊天背景（外观页） ----
// 控件是可选增强：旧 index.html 没有这些节点时其它设置照常工作（与外观控件同款约定）。
export function populateChatBackgroundSettings() {
  const configured = state.bootstrap?.settings?.chat_background || {};
  const previous = state.chatBackground || {};
  // 取景字段必须一起带过去：漏传的表现是"面板一开，用户调好的取景被回填成默认"。
  // crop 用键存在与否判断（它的"未设置"是 null，?? 会把 null 吞掉）。
  applyChatBackground({
    image: configured.image ?? previous.image ?? '',
    opacity: configured.opacity ?? previous.opacity,
    crop: 'crop' in configured ? configured.crop : (previous.crop ?? null),
    position_x: configured.position_x ?? previous.position_x,
    position_y: configured.position_y ?? previous.position_y,
    zoom: configured.zoom ?? previous.zoom,
  });
  updateChatBackgroundControls();
  setChatBackgroundStatus('');
  // 打开设置页顺手重探一次当前背景图：文件被清理/补回后不必重启就能看到准确状态
  // （启动探针只跑一次；探针失败**不会**改设置，只刷新「文件暂不可用」提示）。
  void refreshChatBackgroundImageStatus();
}

// 面板回显的唯一写入点：滑杆值/百分比/缩略图显隐/清除与调整按钮可用性都随当前背景状态走。
export function updateChatBackgroundControls() {
  const background = state.chatBackground || { image: '', opacity: 0.35 };
  const slider = $('#chatBackgroundOpacity');
  // 用户正在拖滑杆时不要用回填值抢走手柄（input 事件会重绘周边文案）。
  if (slider && document.activeElement !== slider) slider.value = String(background.opacity);
  syncRangeFill(slider);
  const output = $('#chatBackgroundOpacityValue');
  if (output) output.textContent = `${Math.round(Number(background.opacity) * 100)}%`;
  const preview = $('#chatBackgroundPreview');
  if (preview) preview.hidden = !background.image;
  // 「文件暂不可用」提示：设置**原样保留**（探针失败只标记、不清空），但必须让用户看得见——
  // 背景一片空却没有任何说明就是静默失效；这里给出原因与出路（重新选择图片）。
  const imageMissing = Boolean(state.chatBackgroundImageMissing) && Boolean(background.image);
  const missingHint = $('#chatBackgroundMissingHint');
  if (missingHint) {
    missingHint.hidden = !imageMissing;
    missingHint.textContent = imageMissing
      ? '背景图文件暂不可用（设置已保留）：图片可能已被缓存清理或数据目录迁移移除，点「更换图片」重新选择即可。'
      : '';
  }
  const previewLabel = preview?.querySelector('.chat-background-preview-label');
  if (previewLabel) previewLabel.textContent = imageMissing ? '文件暂不可用' : '点击调整';
  const clear = $('#clearChatBackground');
  if (clear) clear.disabled = !background.image;
  const pick = $('#pickChatBackground');
  if (pick) pick.textContent = background.image ? '更换图片' : '选择图片';
  const edit = $('#editChatBackground');
  if (edit) {
    // 没图时"调整取景"没有对象；编辑器里的失效提示另有一条（这里是入口的先决条件）。
    edit.disabled = !background.image;
    edit.title = background.image ? '在白板上拖动 / 缩放，决定露出图片的哪一块' : '请先选择一张背景图';
  }
  syncChatBackgroundPresetState();
}

// ---- 内置背景图（设置卡那一排缩略图） ----
// 清单来自 /api/backgrounds（首次访问时后端幂等生成到 data/backgrounds/），点一下即应用。
export async function loadChatBackgroundPresets() {
  if (state.chatBackgroundPresets?.length) {
    renderChatBackgroundPresets(state.chatBackgroundPresets);
    return state.chatBackgroundPresets;
  }
  try {
    const result = await api('/api/backgrounds');
    state.chatBackgroundPresets = Array.isArray(result?.presets) ? result.presets : [];
  } catch (error) {
    // 拿不到内置图只是少一个便利入口，不打扰用户；控制台留一条供排查（warn 不是 error，
    // 零报错的冒烟断言不受影响）。
    state.chatBackgroundPresets = [];
    console.warn('内置背景图清单读取失败：', error.message);
  }
  renderChatBackgroundPresets(state.chatBackgroundPresets);
  return state.chatBackgroundPresets;
}

export function renderChatBackgroundPresets(presets = []) {
  const box = $('#chatBackgroundPresets');
  const row = $('#chatBackgroundPresetsRow');
  if (!box || !row) return;
  const list = Array.isArray(presets) ? presets.filter((item) => item?.path) : [];
  if (!list.length) {
    box.hidden = true;
    row.replaceChildren();
    return;
  }
  const current = String(state.chatBackground?.image || '');
  row.innerHTML = list.map((preset) => {
    const path = String(preset.path);
    const active = Boolean(current) && path === current;
    const thumb = String(preset.thumb_path || path);
    const name = escapeHtml(preset.name || '内置背景');
    return `<button type="button" class="chat-background-preset${active ? ' is-active' : ''}" role="radio" aria-checked="${active ? 'true' : 'false'}" data-chat-bg-preset="${escapeHtml(path)}" title="${escapeHtml(preset.description || preset.name || '')}"><img src="${escapeHtml(localFileUrl(thumb))}" alt="" loading="lazy"><span>${name}</span></button>`;
  }).join('');
  box.hidden = false;
}

// 只切换"当前用的是哪一张"的高亮：整排重绘会丢焦点、也会在拖滑杆时反复重建 DOM。
export function syncChatBackgroundPresetState() {
  const current = String(state.chatBackground?.image || '');
  $$('#chatBackgroundPresetsRow .chat-background-preset').forEach((button) => {
    const active = Boolean(current) && button.dataset.chatBgPreset === current;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-checked', active ? 'true' : 'false');
  });
}

// 缩略图文件缺失（生成失败/被删）时别留破图：把 img 摘掉，按钮降级成纯文字。
document.addEventListener('error', (event) => {
  const img = event.target;
  if (!(img instanceof HTMLImageElement) || !img.closest('.chat-background-preset')) return;
  img.remove();
}, true);

// ---- 背景图编辑器（弹层） ----
// 弹层是可选增强：旧 index.html 没有这些节点时整块跳过。

// 概览视图：整张图按 contain 铺满可用区，取景框按 crop 画在图上。
// 为什么全部用 px：比例与位置必须是准的，不能靠 aspect-ratio + max-*（Chromium 为了满足
// 上限会破坏比例 → 取景就不准了，上一轮实测踩过）。
// 取景框永远在图片内（clampChatBackgroundCrop 保证），所以"整图 ∪ 框"就是图片本身——
// 旧版那套并集求解整个不需要了，也不会再出现"框跑到图外把概览撑成奇怪比例"。
export function renderChatBackgroundCropView() {
  const stage = $('#chatBgStage');
  const overview = $('#chatBgOverview');
  const imageEl = $('#chatBgOverviewImage');
  const frameEl = $('#chatBgCropFrame');
  if (!stage || !overview || !imageEl || !frameEl) return;
  const aspect = chatBackgroundImageAspect();
  if (!(aspect > 0)) return;   // 图片比例还没探到：保持现状（失效提示另有出口）
  const crop = chatBackgroundCrop(state.chatBackground || {});
  const style = window.getComputedStyle(stage);
  const availableWidth = Math.max(
    40, stage.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
  );
  const availableHeight = Math.max(
    40, stage.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom),
  );
  const imageWidth = Math.min(availableWidth, availableHeight * aspect);
  const imageHeight = imageWidth / aspect;
  const px = (value) => `${value.toFixed(2)}px`;
  overview.style.width = px(imageWidth);
  overview.style.height = px(imageHeight);
  imageEl.style.left = '0px';
  imageEl.style.top = '0px';
  imageEl.style.width = px(imageWidth);
  imageEl.style.height = px(imageHeight);
  frameEl.style.left = px(crop.x * imageWidth);
  frameEl.style.top = px(crop.y * imageHeight);
  frameEl.style.width = px(crop.w * imageWidth);
  frameEl.style.height = px(crop.h * imageHeight);
}

// 打开编辑器时调用：概览尺寸 + 缩放滑杆的动态上下限 + 回填当前取景。
export function populateChatBackgroundEditor() {
  if (!$('#chatBackgroundBoard')) return;
  const limits = chatBackgroundCropScaleLimits();
  const slider = $('#chatBgZoom');
  if (slider) {
    slider.min = String(limits.min);
    slider.max = String(limits.max);
    slider.step = '0.01';
  }
  renderChatBackgroundCropView();
  updateChatBackgroundEditorControls();
  setChatBackgroundEditorError('');
  setChatBackgroundEditorEnabled(true);
}

// 状态 → 界面的单向回填（拖框、拖手柄、滚轮、滑杆、预置、旋转都走它）。
export function updateChatBackgroundEditorControls() {
  const scale = chatBackgroundCropScale(chatBackgroundCrop(state.chatBackground || {}));
  const limits = chatBackgroundCropScaleLimits();
  const slider = $('#chatBgZoom');
  if (slider) {
    slider.min = String(limits.min);
    slider.max = String(limits.max);
    // 不要把用户正在拖的手柄抢走（值相同则不写）。
    if (Math.abs(Number(slider.value) - scale) > 0.005) slider.value = String(scale);
  }
  const output = $('#chatBgZoomValue');
  if (output) output.textContent = `${Math.round(scale * 100)}%`;
  renderChatBackgroundCropView();
}

export function setChatBackgroundEditorEnabled(enabled) {
  ['#chatBgZoom', '#chatBgFitCover', '#chatBgFitContain', '#chatBgReset', '#chatBgRotate', '#chatBgSave']
    .forEach((selector) => {
      const element = $(selector);
      if (element) element.disabled = !enabled;
    });
}

export function setChatBackgroundEditorError(message) {
  const box = $('#chatBgEditorError');
  if (!box) return;
  box.textContent = String(message || '');
  box.hidden = !message;
}

export function setChatBackgroundStatus(message, isError = false) {
  const status = $('#chatBackgroundStatus');
  if (!status) return;
  status.textContent = String(message || '');
  // `.settings-content .hint` 是 (0,2,0)，普通类压不住它；错误色用 id 级规则。
  status.classList.toggle('is-error', Boolean(isError) && Boolean(message));
}

export function updateSkillSummary() {
  const fixedCount = currentAgentFixedSkillIds().length;
  // 只填数字：按钮自带「Skill」文字标签（此前填「Skill N」→ 顶栏显示「Skill 8 Skill」）。
  $('#skillCount').textContent = String(state.bootstrap.skills.length);
  $('#skillPolicyHint').textContent = '点击某项即在输入框光标处插入 /技能 引用；发送后按“首轮注入 / 后续追加”注入';
  $('#skillsSummary').textContent = `${state.bootstrap.skills.length} 个可用，当前 Agent 预设 ${fixedCount} 个（新建会话自动预填引用）`;
}

/* ---------- API 供应商：卡片列表 + 点开才弹出的设置对话框 ---------- */

// 请求格式 → 卡片类型标签（与表单下拉同一套格式名，避免两处各写各的）。
const PROVIDER_FORMAT_LABELS = {
  openai_chat: 'OpenAI 兼容',
  codex_responses: 'Codex /responses',
  gemini: 'Gemini',
  claude: 'Claude',
  lm_studio: 'LM Studio',
  ollama: 'Ollama',
  llama_cpp: 'llama.cpp',
  unsloth: 'Unsloth',
};

function providerProfiles() {
  return state.bootstrap?.model_profiles || state.bootstrap?.providers || [];
}

/* ---------- 供应商模板（预设）：卡片网格，选中即回填连接字段 ---------- */

// 卡片首字色块：按预设下标循环取色；色块底/字都用 color-mix 与主题面、字混色，
// 亮暗主题与各皮肤都成立（不引品牌 logo 图片，离线可用、发版不缺图）。
export const PROVIDER_PRESET_ACCENTS = ['#4C6EF5', '#8B5CF6', '#0FA97F', '#E8930C', '#14A0C4', '#D0454A', '#5A67D8', '#C2557A'];
const PRESET_GUIDE_FALLBACK = '选择上方任一模板可自动填好 API URL 与请求格式，只需再粘贴 API Key；也可以直接手填下面的字段。';

export function providerPresets() {
  return Array.isArray(state.providerPresetList) ? state.providerPresetList : [];
}

export function providerPresetById(presetId) {
  const key = String(presetId || '').trim();
  return key ? providerPresets().find((preset) => preset.id === key) || null : null;
}

// 名单唯一来源是后端 /api/provider-presets：拿不到时（离线/旧服务）返回空表，
// 弹层退化成全手填路径，不抛错、不阻塞打开设置。
export async function loadProviderPresets() {
  if (providerPresets().length) return providerPresets();
  try {
    const result = await api('/api/provider-presets');
    state.providerPresetList = Array.isArray(result.presets) ? result.presets : [];
  } catch (_) {
    state.providerPresetList = [];
  }
  return providerPresets();
}

// 卡片副标题：本地后端与自定义没有推荐模型，用「免 Key / 手动填写」说明这张卡给的是什么。
export function providerPresetSubtitle(preset) {
  if (!preset.key_required) return '免 Key';
  return String(preset.model || '') || '手动填写';
}

export function providerPresetCardMarkup(preset, index, activeId = '') {
  const accent = PROVIDER_PRESET_ACCENTS[index % PROVIDER_PRESET_ACCENTS.length];
  const active = preset.id === activeId;
  const name = escapeHtml(preset.name || preset.id || '');
  const abbr = String(preset.abbr || (preset.name || '?')).slice(0, 3);
  return `
    <button type="button" class="provider-preset-card${active ? ' is-active' : ''}" data-provider-preset="${escapeHtml(preset.id)}" aria-pressed="${active ? 'true' : 'false'}" style="--preset-accent:${accent}" title="${name}">
      <span class="provider-preset-abbr" aria-hidden="true">${escapeHtml(abbr)}</span>
      <span class="provider-preset-meta">
        <span class="provider-preset-name">${name}</span>
        <span class="provider-preset-sub">${escapeHtml(providerPresetSubtitle(preset))}</span>
      </span>
    </button>`;
}

// 网格渲染：设置弹层与首启向导共用（同一个空容器 id 由调用方给，高亮各自传各自的选中项）。
export function renderProviderPresetGrid(containerId, activeId = '') {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = providerPresets().map((preset, index) => providerPresetCardMarkup(preset, index, activeId)).join('');
}

// 只翻高亮、不重排 DOM（点卡回填表单时用，避免顺手把滚动位置弹回顶部）。
export function markProviderPresetCards(containerId, activeId = '') {
  const scope = document.getElementById(containerId);
  if (!scope) return;
  scope.querySelectorAll('[data-provider-preset]').forEach((card) => {
    const active = Boolean(activeId) && card.dataset.providerPreset === activeId;
    card.classList.toggle('is-active', active);
    card.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

// 「还有 …」提示：网格只露一部分时，把剩下没露脸的模板名字列出来（名字也来自同一份名单）。
export function providerPresetMoreText(visibleCount) {
  const presets = providerPresets();
  if (!presets.length || presets.length <= visibleCount) return '';
  const rest = presets.slice(visibleCount).map((preset) => preset.name);
  const head = rest.slice(0, 5).join('、');
  return `还有 ${head}${rest.length > 5 ? ' 等' : ''}，共 ${presets.length} 个模板（可滚动查看）。`;
}

// 引导条：选中模板后显示「这是什么站 + 怎么注册拿 Key」，附「打开注册页」按钮；
// 没选模板时给默认说明（向导里传 fallback='' 表示整条隐藏）。
export function fillProviderPresetGuide(root, preset, { fallback = PRESET_GUIDE_FALLBACK } = {}) {
  if (!root) return;
  const text = root.querySelector('.provider-preset-guide-text');
  const button = root.querySelector('.provider-preset-key');
  const hint = preset ? String(preset.hint || '') : fallback;
  root.hidden = !hint;
  if (text) text.textContent = hint;
  if (button) {
    button.hidden = !(preset && preset.key_url);
    button.dataset.presetId = preset ? String(preset.id) : '';
  }
}

// 高亮当前模板（编辑老卡片时按卡里记的 preset_id 反显；没有来源就不高亮）。
// containerId 指到哪个网格就只标哪个网格：设置弹层与首启向导各有一份卡片。
export function setProviderPresetSelection(presetId, containerId = 'providerPresetGrid') {
  const key = String(presetId || '').trim();
  // 名单还没载入时先原样记住（拉回来后再反显）；名单已到时只认表内的 id。
  state.providerPresetId = (!providerPresets().length || providerPresetById(key)) ? key : '';
  markProviderPresetCards(containerId, state.providerPresetId);
}

// 名单拉回来后补渲染（启动竞态：弹层可能先于 /api/provider-presets 打开）。
function refreshProviderPresetGrid() {
  void loadProviderPresets().then(() => {
    renderProviderPresetGrid('providerPresetGrid', state.providerPresetId);
    fillProviderPresetGuide($('#providerPresetGuide'), providerPresetById(state.providerPresetId));
  });
}

// 选中模板：把名称/地址/请求格式/推荐模型/类型填进表单，并切引导文案。
// 只写「模板默认值」，之后用户手改的字段不会再被覆盖（保存时 preset_id 只作来源标记）。
export function applyProviderPreset(presetId) {
  const preset = providerPresetById(presetId);
  if (!preset) return null;
  const local = preset.kind === 'local';
  $('#providerKind').value = local ? '1' : '0';
  $('#providerName').value = preset.name || '';
  $('#providerBaseUrl').value = preset.base_url || '';
  $('#providerFormat').value = preset.request_format || 'openai_chat';
  syncProviderKindOptions();
  setProviderModelOptions([], preset.model || '');
  updateProviderFormatGuide();
  updateProviderContextField();
  updateUnloadModelButton();
  setProviderPresetSelection(preset.id);
  fillProviderPresetGuide($('#providerPresetGuide'), preset);
  return preset;
}

// 「打开注册页」：地址由服务端按预设白名单取（前端只传 preset_id），
// 用系统默认浏览器打开；失败就把原因说清楚，不做 window.open 兜底（内嵌窗口会再开一屏）。
export async function openProviderPresetKeyUrl(presetId) {
  const preset = providerPresetById(presetId);
  if (!preset?.key_url) return;
  try {
    await api('/api/provider-presets/open', { method: 'POST', body: { preset_id: preset.id } });
  } catch (error) {
    toast(`打开注册页失败：${error.message}（可手动访问 ${preset.key_url}）`);
  }
}

function providerCardMarkup(provider) {
  const id = escapeHtml(provider.id || '');
  const label = provider.name || '未命名供应商';
  const name = escapeHtml(label);
  const initial = escapeHtml((Array.from(label.trim())[0] || '?').toUpperCase());
  const format = PROVIDER_FORMAT_LABELS[provider.request_format] || escapeHtml(provider.request_format || '未指定格式');
  // 卡片压成两行：首行（首字头像 + 名称）+ 脚行（格式标签 + 当前角标）。模型名不再上卡片（在弹层里看），
  // 卡片更矮、一行能放下的信息更整齐。头像底色走 --accent-soft（原版配色，不引入新色值）。
  return `
    <div class="provider-card${provider.is_default ? ' is-default' : ''}" data-provider-card="${id}" role="button" tabindex="0" aria-label="编辑 ${name}">
      <button class="provider-card-delete" type="button" data-provider-delete="${id}" title="删除 ${name}" aria-label="删除 ${name}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>
      </button>
      <span class="provider-card-head">
        <span class="provider-card-avatar" aria-hidden="true">${initial}</span>
        <span class="provider-card-name" title="${name}">${name}</span>
      </span>
      <span class="provider-card-foot">
        <span class="provider-card-tag">${format}</span>
        ${provider.is_default ? '<span class="provider-card-badge">当前</span>' : ''}
      </span>
    </div>`;
}

export function renderProviders() {
  const providers = providerProfiles().filter((provider) => (provider.kind || 'online') === state.providerKindTab);
  $$('[data-provider-kind]').forEach((button) => {
    const active = button.dataset.providerKind === state.providerKindTab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  const container = $('#providerCards');
  if (!container) return;
  // 「添加 API」卡片固定排在最后一张（列表为空时它就是唯一一张卡）。
  container.innerHTML = providers.map(providerCardMarkup).join('') + `
    <button type="button" class="provider-card provider-card-add" data-provider-add>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg>
      <span>添加 API</span>
    </button>`;
}

export function openProviderCard(providerId) {
  const provider = providerProfiles().find((item) => item.id === providerId);
  if (!provider) return;
  showProviderForm(provider);
}

export function showProviderForm(provider = {}, { isNew = false } = {}) {
  $('#providerId').value = provider.id || '';
  $('#providerName').value = provider.name || '';
  $('#providerBaseUrl').value = provider.base_url || '';
  const inferredKind = provider.kind || (['ollama', 'lm_studio', 'llama_cpp', 'unsloth'].includes(provider.request_format) ? 'local' : 'online');
  $('#providerKind').value = inferredKind === 'local' ? '1' : '0';
  $('#providerContextWindow').value = provider.context_window || provider.context_size || '';
  $('#providerMaxOutputTokens').value = provider.max_output_tokens || '';
  $('#providerTemperature').value = provider.temperature ?? '';
  $('#providerReasoningEffort').value = provider.reasoning_effort || 'auto';
  $('#providerSupportsImages').value = provider.supports_images_explicit === true
    ? 'true'
    : (provider.supports_images_explicit === false ? 'false' : 'auto');
  setProviderModelOptions([], provider.model || '');
  $('#providerFormat').value = provider.request_format || 'openai_chat';
  $('#providerApiKey').value = '';
  $('#providerApiKey').type = 'password';
  $('#toggleProviderKey').textContent = '显示';
  $('#toggleProviderKey').title = '显示 API Key';
  $('#providerKeyStatus').textContent = provider.has_api_key ? '已配置' : '未配置';
  $('#providerError').textContent = '';
  $('#providerDialogTitle').textContent = isNew ? '添加 API' : (provider.name || 'API 供应商');
  $('#providerDialogSubtitle').textContent = isNew
    ? '选择供应商模板，选好后只需粘贴 API Key'
    : (inferredKind === 'local' ? '本地 API' : '在线 API');
  // 卡片点开即可编辑（不再有「只读 → 点编辑」两态）。
  setProviderEditMode(true);
  // 模板网格：新卡不高亮任何模板；老卡按卡里记的来源反显（没记来源就是全手填配置）。
  setProviderPresetSelection(provider.preset_id || '');
  renderProviderPresetGrid('providerPresetGrid', state.providerPresetId);
  fillProviderPresetGuide($('#providerPresetGuide'), providerPresetById(state.providerPresetId));
  if (!providerPresets().length) refreshProviderPresetGrid();
  syncProviderKindOptions();
  updateProviderFormatGuide();
  updateProviderContextField();
  updateProviderVisionHint();
  updateUnloadModelButton();
  const dialog = $('#providerDialog');
  if (dialog && !dialog.open) dialog.showModal();
  $('#providerName').focus();
}

export function syncProviderKindOptions(previousFormat = '') {
  const local = $('#providerKind').value === '1';
  const allowed = local ? ['lm_studio', 'ollama', 'llama_cpp', 'unsloth'] : ['openai_chat', 'codex_responses', 'gemini', 'claude'];
  const format = $('#providerFormat');
  [...format.options].forEach((option) => { option.hidden = !allowed.includes(option.value); });
  if (!allowed.includes(format.value)) {
    // A legacy llama.cpp endpoint was commonly configured as an online
    // OpenAI-compatible API. Switching its type to local must keep the
    // /v1 protocol instead of silently redirecting it to LM Studio's /api/v1.
    format.value = local && previousFormat === 'openai_chat' ? 'llama_cpp' : allowed[0];
  }
  const hint = $('#providerKindHint');
  if (hint) hint.textContent = local ? '本地 API 可使用 llama.cpp、Unsloth、Ollama 或 LM Studio 服务。' : '在线 API 使用远程模型服务。';
}

export function updateProviderFormatGuide() {
  const guide = $('#providerFormatGuide');
  const format = $('#providerFormat').value;
  const guides = {
    ollama: '先启动 Ollama。API URL 通常填写 http://127.0.0.1:11434/v1；API Key 可留空；模型名称可通过 ollama list 查看，然后点击“检查模型”。',
    lm_studio: '先在 LM Studio 的 Developer / Local Server 页面启动服务并加载模型。API URL 通常填写 http://127.0.0.1:1234/v1；API Key 可留空，然后点击“检查模型”。',
    llama_cpp: '先启动 llama.cpp server。API URL 通常填写 http://127.0.0.1:8080/v1；API Key 可留空，然后点击“检查模型”。',
    unsloth: '先启动 Unsloth（桌面版或 unsloth studio）。API URL 通常填写 http://127.0.0.1:8000 或 http://127.0.0.1:8888；API Key 在 Unsloth Settings → API 创建（sk-unsloth-…）；上下文长度由启动参数 unsloth run -c <tokens> 决定。然后点击“检查模型”。',
  };
  guide.textContent = guides[format] || '';
  guide.hidden = !guides[format];
}

export function updateProviderContextField() {
  ['#providerContextWindow', '#providerMaxOutputTokens', '#providerTemperature'].forEach((selector) => {
    const element = $(selector);
    if (element) element.disabled = !state.providerEditing;
  });
}

export function setProviderEditMode(editing) {
  state.providerEditing = editing;
  [
    '#providerName', '#providerBaseUrl', '#providerApiKey', '#providerFormat',
    '#providerKind', '#providerModel', '#providerModelCustom', '#providerContextWindow',
    '#providerMaxOutputTokens', '#providerTemperature', '#providerReasoningEffort',
    '#providerSupportsImages', '#loadProviderModels',
  ].forEach((selector) => {
    const element = $(selector);
    if (element) element.disabled = !editing;
  });
  updateProviderContextField();
  updateUnloadModelButton();
}

export function setProviderModelOptions(models = [], current = '') {
  const select = $('#providerModel');
  const unique = [];
  const seen = new Set();
  models.forEach((model) => {
    const id = String(model.id || '').trim();
    if (!id || seen.has(id)) return;
    seen.add(id);
    unique.push({ id, name: String(model.name || id) });
    state.providerModelCapabilities[id] = {
      context_window: model.context_window,
      max_output_tokens: model.max_output_tokens,
      supports_images: typeof model.supports_images === 'boolean' ? model.supports_images : undefined,
    };
  });
  if (current && !seen.has(current)) unique.unshift({ id: current, name: current });
  const prompt = unique.length > 1 && !current
    ? `<option value="">请选择模型（${unique.length} 个可用）</option>`
    : '';
  select.innerHTML = unique.length
    ? prompt + unique.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}</option>`).join('')
    : '<option value="">填写连接信息后自动检查</option>';
  select.insertAdjacentHTML('beforeend', '<option value="__custom__">手动输入模型名称…</option>');
  select.value = current || (unique.length === 1 ? unique[0].id : '');
  $('#providerModelCustom').hidden = true;
  $('#providerModelCustom').required = false;
}

export function applyProviderModelCapabilities() {
  const model = $('#providerModel').value;
  const capability = state.providerModelCapabilities[model];
  if (!capability) return;
  if (!$('#providerContextWindow').value && capability.context_window) {
    $('#providerContextWindow').value = capability.context_window;
  }
  if (!$('#providerMaxOutputTokens').value && capability.max_output_tokens) {
    $('#providerMaxOutputTokens').value = capability.max_output_tokens;
  }
  updateProviderVisionHint();
}

export function updateProviderVisionHint() {
  const hint = $('#providerVisionHint');
  const choice = $('#providerSupportsImages').value;
  if (choice === 'true') {
    hint.textContent = '已强制设为支持图片；Cat Chat 会把用户图片直接交给该模型。';
    return;
  }
  if (choice === 'false') {
    hint.textContent = '已强制设为纯文本；用户原图不会发送给该模型。';
    return;
  }
  const capability = state.providerModelCapabilities[$('#providerModel').value];
  if (typeof capability?.supports_images === 'boolean') {
    hint.textContent = `模型目录报告：${capability.supports_images ? '支持图片' : '纯文本'}（仍保持自动检测，不写入强制配置）。`;
    return;
  }
  hint.textContent = '自动检测会优先读取运行端能力；上传图片时才会执行最小图片探针。';
}

export function toggleCustomModel() {
  const custom = $('#providerModel').value === '__custom__';
  $('#providerModelCustom').hidden = !custom;
  $('#providerModelCustom').required = custom;
  if (custom) $('#providerModelCustom').focus();
}

export function providerFormValue() {
  const selectedModel = $('#providerModel').value;
  const kind = $('#providerKind').value === '1' ? 'local' : 'online';
  const numberOrUndefined = (selector) => {
    const raw = $(selector).value.trim();
    return raw ? Number(raw) : undefined;
  };
  const imageChoice = $('#providerSupportsImages').value;
  return {
    id: $('#providerId').value,
    // 来源模板（可空）：服务端据此回填空字段，也让卡片记住自己是哪个模板来的。
    preset_id: state.providerPresetId || '',
    name: $('#providerName').value.trim(),
    base_url: $('#providerBaseUrl').value.trim(),
    model: selectedModel === '__custom__' ? $('#providerModelCustom').value.trim() : selectedModel,
    api_key: $('#providerApiKey').value.trim(),
    kind,
    local_backend: kind === 'local' ? $('#providerFormat').value : undefined,
    request_format: $('#providerFormat').value,
    context_window: numberOrUndefined('#providerContextWindow'),
    max_output_tokens: numberOrUndefined('#providerMaxOutputTokens'),
    temperature: numberOrUndefined('#providerTemperature'),
    reasoning_effort: $('#providerReasoningEffort').value,
    supports_images: imageChoice === 'auto' ? null : imageChoice === 'true',
    // 单价**不在这里提交**：定价已迁到「用量统计 → 右上角 费用单价」（按 base_url 分组 →
    // 模型），走 POST /api/providers/pricing 单点写入。不带价格键时后端不动旧值
    // （config.upsert_model_profile 的向后兼容分支），所以这张表单既不会误清也不会覆盖价格。
  };
}

export async function loadProviderModels({ automatic = false } = {}) {
  if (!state.providerEditing) return;
  const values = providerFormValue();
  const localFormat = ['lm_studio', 'ollama', 'llama_cpp', 'unsloth'].includes(values.request_format);
  if (!values.base_url || (!values.api_key && !values.id && !localFormat)) {
    if (!automatic) $('#providerError').textContent = '请先填写 API URL 和 API Key';
    return;
  }
  const button = $('#loadProviderModels');
  const providerKey = `${values.kind === 'local' ? 'local' : 'online'}:${String(values.id || '').trim()}`;
  if (values.id) delete state.providerModelCatalogs[providerKey];
  button.disabled = true;
  button.textContent = '检查中…';
  if (!automatic) $('#providerError').textContent = '正在获取可用模型…';
  try {
    const result = await api('/api/providers/models', { method: 'POST', body: values });
    if (!result.models?.length) throw new Error('接口没有返回可用模型，请检查供应商配置');
    if (values.id) state.providerModelCatalogs[providerKey] = result.models;
    const current = $('#providerModel').value;
    setProviderModelOptions(result.models, current && current !== '__custom__' ? current : '');
    applyProviderModelCapabilities();
    $('#providerError').textContent = '模型目录可访问；请继续点击“测试连接”验证实际推理。';
    if ($('#modelSelect')?.value === providerKey) void populateComposerModels();
    toast(`已找到 ${result.models.length} 个模型`);
  } catch (error) {
    if (values.id) delete state.providerModelCatalogs[providerKey];
    $('#providerError').textContent = `模型检查失败：${error.message}`;
    if (!$('#providerModel').value) setProviderModelOptions([], '');
  } finally {
    button.disabled = false;
    button.textContent = '检查模型';
  }
}

export let providerModelCheckTimer;
export function scheduleProviderModelCheck() {
  clearTimeout(providerModelCheckTimer);
  providerModelCheckTimer = setTimeout(() => loadProviderModels({ automatic: true }), 350);
}

// 保存成功后把服务端返回的卡片并回本地 bootstrap，并刷新模型 / 视觉下拉。
// 设置弹层与首启向导共用：两处都保存 API，刷新动作必须一致（少刷一处就会出现
// 「刚加完供应商但模型下拉里没有它」）。
export function syncSavedProvider(saved) {
  ['providers', 'model_profiles'].forEach((key) => {
    const list = state.bootstrap[key] || (state.bootstrap[key] = []);
    const index = list.findIndex((item) => item.id === saved.id);
    if (index >= 0) list[index] = saved;
    else list.push(saved);
  });
  const visionSelect = $('#visionProvider');
  if (visionSelect) delete visionSelect.dataset.populated;
  populateVisionSettings();
  populateModels();
  // 新卡片默认没有单价 ⇒ 用量页「费用单价」角标要跟着变（不用等切回设置页才发现）。
  refreshUsagePricingBadge();
}

// 保存进行中的重入闸：`#saveProvider` 禁用只能挡住"点按钮"，**挡不住在输入框里按回车**
// （disabled 的提交按钮不会派发 click，但表单仍会 submit）。两个都要。
let providerSaveInFlight = false;

export async function saveProvider(event) {
  event.preventDefault();
  if (providerSaveInFlight) return;
  const button = $('#saveProvider');
  const label = button?.textContent || '保存设置';
  providerSaveInFlight = true;
  if (button) {
    // 与首启向导 saveOnboardingProvider 同款：保存期间禁用 + 「保存中…」，
    // 两处共用 syncSavedProvider，交互态也必须一致（少刷一处就是这次的事故）。
    button.disabled = true;
    button.textContent = '保存中…';
  }
  try {
    const values = providerFormValue();
    if (values.model && (!values.context_window || !values.max_output_tokens)) {
      // 这段预探测可能要几秒到十几秒（在线中转站），旧实现全程零反馈 ⇒
      // 用户以为"点了没反应"就继续点。凡是慢请求前面都要有一句话。
      $('#providerError').textContent = '正在获取模型上下文参数…';
      try {
        const result = await api('/api/providers/models', { method: 'POST', body: values });
        const matched = (result.models || []).find((item) => String(item.id || '') === values.model);
        if (matched?.context_window && !values.context_window) {
          values.context_window = Number(matched.context_window);
          $('#providerContextWindow').value = values.context_window;
        }
        if (matched?.max_output_tokens && !values.max_output_tokens) {
          values.max_output_tokens = Number(matched.max_output_tokens);
          $('#providerMaxOutputTokens').value = values.max_output_tokens;
        }
      } catch (_) {
        // Capability metadata is optional; the provider may supply defaults.
      }
    }
    const saved = await api('/api/providers', { method: 'POST', body: values });
    // 先落"结果"，再刷新：`syncSavedProvider` 的第一步是把卡片并进本地 bootstrap
    // （必须成功，否则关窗后列表里看不到新卡片），后面的 populateModels /
    // populateVisionSettings 只是顺手刷新——旧实现里它们抛错会连 toast 与关窗一起
    // 跳过，用户看到的同样是"点了没反应"。刷新失败只写控制台，不影响保存结论。
    $('#providerId').value = saved.id;
    try {
      syncSavedProvider(saved);
    } catch (refreshError) {
      console.error('[providers] 供应商已保存，但刷新列表失败', refreshError);
    }
    cancelProviderEdit();
    toast('API 供应商已保存');
  } catch (error) {
    // 弹窗可能已被"另一次并发的保存"关掉：那时把错误写进 #providerError 等于丢进
    // 看不见的地方（体感仍是"没反应"）。弹窗还在就原地提示，已关就 toast 出来。
    if ($('#providerDialog')?.open) $('#providerError').textContent = error.message;
    else toast(`保存失败：${error.message}`);
  } finally {
    providerSaveInFlight = false;
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

export function addProvider() {
  const local = state.providerKindTab === 'local';
  showProviderForm({
    kind: state.providerKindTab,
    request_format: local ? 'lm_studio' : 'openai_chat',
  }, { isNew: true });
}

// 关闭设置弹层并复位编辑态；Esc、右上角关闭按钮、取消、保存成功四条路径都走这里。
export function cancelProviderEdit() {
  clearTimeout(providerModelCheckTimer);
  state.providerEditing = false;
  const dialog = $('#providerDialog');
  if (dialog?.open) dialog.close();
  renderProviders();
}

// 卡片右上角 × 的删除入口：按 id 删（不再依赖「先在下拉里选中」）。
export async function deleteProvider(providerId) {
  const provider = providerProfiles().find((item) => item.id === providerId);
  if (!provider) return;
  if (!confirm(`删除供应商“${provider.name || provider.id}”？这会同时移除模型配置。`)) return;
  try {
    await api(`/api/providers/${encodeURIComponent(providerId)}`, { method: 'DELETE' });
    const data = await api('/api/bootstrap');
    state.bootstrap = { ...state.bootstrap, ...data };
    if ($('#providerId').value === providerId) {
      cancelProviderEdit();
    } else {
      renderProviders();
    }
    populateModels();
    populateVisionSettings();
    toast('供应商已删除');
  } catch (error) {
    toast(`删除 API 失败：${error.message}`);
  }
}

// 连接测试结果 → 一行人话（设置弹层与首启向导共用，避免两处各写一套来源名）。
export function providerTestSummary(result) {
  const sourceLabels = {
    explicit: '手动配置',
    llama_props: 'llama.cpp /props',
    ollama_show: 'Ollama capabilities',
    lm_studio_models: 'LM Studio 模型目录',
    image_probe: '真实图片探针',
    model_name: '模型名推断（未确认）',
  };
  const vision = result.supports_images ? '支持图片' : '纯文本';
  const source = sourceLabels[result.capability_source] || result.capability_source || '未知';
  const proxyNote = result.proxy_state?.note ? `；${result.proxy_state.note}` : '';
  return `推理连接成功：${result.response}；视觉：${vision}；来源：${source}${proxyNote}`;
}

export async function testProvider() {
  $('#providerError').textContent = '正在测试连接…';
  try {
    const result = await api('/api/providers/test', { method: 'POST', body: providerFormValue() });
    $('#providerError').textContent = providerTestSummary(result);
  } catch (error) {
    $('#providerError').textContent = `模型目录可能可访问，但推理服务不可用：${error.message}`;
  }
}

export async function toggleProviderKey() {
  const input = $('#providerApiKey');
  const button = $('#toggleProviderKey');
  if (input.type === 'text') {
    input.type = 'password';
    button.textContent = '显示';
    button.title = '显示 API Key';
    return;
  }
  try {
    if (!input.value && $('#providerId').value) {
      const result = await api(`/api/providers/${$('#providerId').value}/secret`);
      input.value = result.api_key || '';
    }
    input.type = 'text';
    button.textContent = '隐藏';
    button.title = '隐藏 API Key';
  } catch (error) {
    $('#providerError').textContent = error.message;
  }
}

export function populateRuntimeSettings() {
  const settings = state.bootstrap.settings;
  if ($('#commandTimeout')) $('#commandTimeout').value = settings.command_timeout;
  if ($('#contextWarningPercent')) {
    $('#contextWarningPercent').value = Number(settings.context_warning_percent ?? 80);
  }
  // 本地首字节超时 / Agent 最大步数：0 有意义（分别表示「关闭本层」「不限制」），
  // 因此不能用 `|| 默认值` 回填，必须原样回显。
  if ($('#localFirstByteTimeout')) {
    $('#localFirstByteTimeout').value = Number(settings.local_first_byte_timeout_seconds ?? 120);
  }
  if ($('#agentStepLimit')) {
    $('#agentStepLimit').value = Number(settings.agent_step_limit ?? 200);
  }
  // 插话直达：布尔偏好，缺省 false = 维持「入队 → 手动引导」两段式（与旧版行为一致）。
  if ($('#interjectDirectSend')) {
    $('#interjectDirectSend').checked = Boolean(settings.interject_direct_send);
  }
  // 思考回放限长同理：0 表示「关闭限长」，不能用 `|| 默认值` 回填。
  if ($('#reasoningReplayMaxChars')) {
    $('#reasoningReplayMaxChars').value = Number(settings.reasoning_replay_max_chars ?? 4000);
  }
  if ($('#reasoningReplayTurnChars')) {
    $('#reasoningReplayTurnChars').value = Number(settings.reasoning_replay_turn_chars ?? 16000);
  }
  // 图片编码记忆同上：0 = 关闭记忆，不能用 `|| 默认值` 回填。
  if ($('#imageEncodeCacheMb')) {
    $('#imageEncodeCacheMb').value = Number(settings.image_encode_cache_mb ?? 512);
  }
  // 新会话种子模板：留空 = 前端回退到内置默认（占位符说明见设置项下方小字）。
  if ($('#contextResetSeedTemplate')) {
    $('#contextResetSeedTemplate').value = String(settings.context_reset_seed_template || '');
  }
  if ($('#workspaceDir')) $('#workspaceDir').value = settings.workspace_dir === 'workspace' ? '' : (settings.workspace_dir || '');
  if ($('#resolvedWorkspaceDir')) $('#resolvedWorkspaceDir').textContent = state.bootstrap.resolved_workspace_dir || '-';
  const imaging = settings.imaging || {};
  if ($('#imageUploadOriginal')) $('#imageUploadOriginal').checked = Boolean(imaging.image_upload_original);
  if ($('#imageMaxPixels')) $('#imageMaxPixels').value = Number(imaging.image_max_pixels || 2000000);
  if ($('#thumbnailMaxPixels')) $('#thumbnailMaxPixels').value = Number(imaging.thumbnail_max_pixels || 500000);
  if ($('#autoCleanLimitMb')) $('#autoCleanLimitMb').value = Number(imaging.auto_clean_limit_mb ?? 256);
  if ($('#generatedCleanLimitMb')) $('#generatedCleanLimitMb').value = Number(imaging.generated_clean_limit_mb ?? 512);
  renderImageCompressRow();
  renderCacheSizes(state.bootstrap || {});
  renderProxySettings();
  renderWorkspaceControl();
}

// ---- Skill 大小上限（设置 → Skills 管理）----

// 回显「Skill 大小上限」输入框并刷新导入对话框的上限文案。
// 0 有意义（= 内置默认），不能用 `|| 默认值` 回填，必须原样回显。
export function populateSkillSizeSetting() {
  const settings = state.bootstrap?.settings || {};
  if ($('#skillMaxSize')) $('#skillMaxSize').value = Number(settings.skill_max_size_mb ?? 0);
  renderSkillSizeLimits();
}

// 导入对话框副标题与预检共用的生效上限（服务端 skill_size_limits 下发的口径，前端不推算）。
export function skillSizeLimits() {
  return state.bootstrap?.settings?.skill_size_limits || null;
}

export function renderSkillSizeLimits() {
  const limits = skillSizeLimits();
  const hint = $('#skillImportLimitHint');
  if (hint) {
    hint.textContent = limits
      ? `（当前上限：压缩包 ${limits.zip_mb} MB / 文件夹 ${limits.folder_mb} MB，可在 设置 → Skills 管理 调整）`
      : '';
  }
}

// 保存「Skill 大小上限」：POST /api/settings 单项提交，以服务端回声为准
// （回声里带 skill_size_limits 生效值时整体替换，保证文案与后端口径一致）。
export async function saveSkillMaxSize() {
  const input = $('#skillMaxSize');
  const resultEl = $('#skillMaxSizeResult');
  if (!input) return;
  const raw = String(input.value ?? '').trim();
  const value = raw === '' ? 0 : Number(raw);
  try {
    const result = await api('/api/settings', { method: 'POST', body: { skill_max_size_mb: value } });
    const saved = result?.settings?.skill_max_size_mb;
    const applied = typeof saved === 'number' ? saved : value;
    if (state.bootstrap?.settings) {
      state.bootstrap.settings.skill_max_size_mb = applied;
      if (result?.settings?.skill_size_limits) {
        state.bootstrap.settings.skill_size_limits = result.settings.skill_size_limits;
      }
    }
    if ($('#skillMaxSize')) $('#skillMaxSize').value = applied;
    renderSkillSizeLimits();
    if (resultEl) {
      resultEl.textContent = applied > 0
        ? `已保存：所有 Skill 安装链路上限 ${applied} MB`
        : '已保存：使用内置默认上限（zip 包 80 MB / 文件夹 300 MB / 解压后 500 MB / AI 帮装 50 MB）';
    }
    toast('Skill 大小上限已保存');
  } catch (error) {
    if (resultEl) resultEl.textContent = `保存失败：${error.message}`;
    toast(`保存失败：${error.message}`);
  }
}

/**
 * 「插话直达」开关：即时生效（与「侧栏分组与排序」同款——POST /api/settings 单项提交，
 * 服务端 settings 是唯一事实来源，不自造 localStorage 影子副本）。
 *
 * checkbox 已经由用户点成新值，写失败必须**回滚**：否则界面显示「已开启」而服务端仍是
 * 关闭，下一轮插话照样排队等引导，用户只会以为开关失灵（「看着开了其实没开」最难查）。
 */
export async function saveInterjectDirectSend(enabled) {
  const value = Boolean(enabled);
  try {
    const result = await api('/api/settings', { method: 'POST', body: { interject_direct_send: value } });
    // 以服务端回声为准（拿不到回声的只有一个可能：对端是不含该键的旧版，退回本次意图值）。
    const saved = result?.settings?.interject_direct_send;
    const applied = typeof saved === 'boolean' ? saved : value;
    if (state.bootstrap?.settings) state.bootstrap.settings.interject_direct_send = applied;
    if ($('#interjectDirectSend')) $('#interjectDirectSend').checked = applied;
    toast(applied ? '插话将直达 AI：下一步即生效' : '插话将先进队列，等你点「引导」');
    return applied;
  } catch (error) {
    if ($('#interjectDirectSend')) $('#interjectDirectSend').checked = !value;
    toast(`保存失败：${error.message}`);
    return !value;
  }
}

/**
 * 完成提示音 / 托盘完成卡片：即时生效（与「插话直达」同款——POST /api/settings 单项提交，
 * checkbox 已经由用户点成新值，写失败必须回滚；服务端 settings 是唯一事实来源）。
 * 走 appearance 段整体提交（saveAppearance），后端白名单校验新键。
 */
export async function saveDoneSound(enabled) {
  const value = Boolean(enabled);
  const previous = state.appearance?.done_sound !== false;
  try {
    await saveAppearance({ done_sound: value });
    if ($('#doneSoundToggle')) $('#doneSoundToggle').checked = value;
    toast(value ? '完成提示音已开启' : '完成提示音已关闭');
    return value;
  } catch (error) {
    // saveAppearance 失败时本地保留的是乐观新值；功能开关必须回滚旧值，
    // 否则"看着开了其实没开"（与「插话直达」同款教训）。
    applyAppearance({ ...state.appearance, done_sound: previous });
    syncAppearanceControls();
    toast(`保存失败：${error.message}`);
    return previous;
  }
}

export async function saveDoneSoundVolume(percent) {
  const raw = Number(percent);
  const value = Number.isFinite(raw) ? Math.min(100, Math.max(0, Math.round(raw))) : 60;
  const previous = Number(state.appearance?.done_sound_volume) || 60;
  try {
    await saveAppearance({ done_sound_volume: value });
    syncAppearanceControls();
    return value;
  } catch (error) {
    applyAppearance({ ...state.appearance, done_sound_volume: previous });
    syncAppearanceControls();
    toast(`保存失败：${error.message}`);
    return previous;
  }
}

export async function saveTrayDoneToast(enabled) {
  const value = Boolean(enabled);
  const previous = state.appearance?.tray_done_toast !== false;
  try {
    await saveAppearance({ tray_done_toast: value });
    if ($('#trayDoneToastToggle')) $('#trayDoneToastToggle').checked = value;
    toast(value ? '托盘完成卡片已开启' : '托盘完成卡片已关闭');
    return value;
  } catch (error) {
    applyAppearance({ ...state.appearance, tray_done_toast: previous });
    syncAppearanceControls();
    toast(`保存失败：${error.message}`);
    return previous;
  }
}

/** 「试听」：无视开关直接播一声当前音量的提示音（挑音量时不用先开开关）。 */
export function previewDoneSound() {
  playDoneSound(Number(state.appearance?.done_sound_volume) || 60, { force: true });
}

/* ---------- 网络代理（出站请求） ---------- */
export function proxyStateFromConfig(proxy) {
  if (!proxy) return { mode: 'system', url: '' };
  const url = String(proxy.url || '').trim();
  if (url) return { mode: 'manual', url };
  if (!proxy.enabled) return { mode: 'direct', url: '' };
  // 开启代理但未填地址：按 use_system_fallback 决定跟随系统代理或直连。
  return proxy.use_system_fallback === false ? { mode: 'direct', url: '' } : { mode: 'system', url: '' };
}

export function renderProxySettings() {
  const settings = state.bootstrap.settings || {};
  const st = proxyStateFromConfig(settings.proxy);
  if ($('#proxySystem')) $('#proxySystem').checked = st.mode === 'system';
  if ($('#proxyDirect')) $('#proxyDirect').checked = st.mode === 'direct';
  if ($('#proxyManual')) $('#proxyManual').checked = st.mode === 'manual';
  if ($('#proxyUrl')) $('#proxyUrl').value = st.url || '';
  renderProxyRows();
  renderProxyStateHint(null);
}

export function renderProxyRows() {
  const row = $('#proxyManualRow');
  if (row) row.hidden = !Boolean($('#proxyManual')?.checked);
}

export function renderProxyStateHint(result) {
  const hint = $('#proxyStateHint');
  if (!hint) return;
  const stateInfo = (result && result.proxy_state) || state.bootstrap?.proxy_state || null;
  if (stateInfo && stateInfo.note) {
    hint.textContent = `当前生效：${stateInfo.note}`;
    return;
  }
  const hasProxy = Boolean((state.bootstrap.settings || {}).proxy);
  hint.textContent = hasProxy
    ? ''
    : '尚未保存过代理开关：外部请求默认跟随系统代理；保存下方选择后立即生效。';
}

export function renderImageCompressRow() {
  const row = $('#imageCompressRow');
  if (row) row.hidden = Boolean($('#imageUploadOriginal')?.checked);
}

export function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let n = value;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

/* 缓存字节数按 scope 分开显示（uploads / generated），另给一行合计。
   `payload` 来自 /api/bootstrap、/api/settings 或 /api/imaging/stats，三处字段同名；
   只有 /api/imaging/stats 会带 `cache_clean`（最近一次清理的如实回报）。 */
export function renderCacheSizes(payload) {
  const data = payload || {};
  const uploads = Number(data.uploads_cache_bytes || 0);
  const generated = Number(data.generated_cache_bytes || 0);
  const total = data.image_cache_bytes !== undefined
    ? Number(data.image_cache_bytes || 0)
    : uploads + generated;
  state.bootstrap.image_cache_bytes = total;
  if ($('#uploadsCacheSize')) $('#uploadsCacheSize').textContent = formatBytes(uploads);
  if ($('#generatedCacheSize')) $('#generatedCacheSize').textContent = formatBytes(generated);
  if ($('#imageCacheSize')) $('#imageCacheSize').textContent = formatBytes(total);
  renderCacheCleanHints(data.cache_clean);
}

/* 每个 scope 的「被引用而无法释放」提示：只在真跑过清理之后才有值
   （后端刻意不做"打开设置页就全表扫一遍"的昂贵动作）。
   按计划 F 的口径：**被引用部分 > 该目录阈值**时才提示，并给出可操作的出路
   （需删除对应会话才能腾出）——否则"点了清理却没腾出多少空间"会被当成按钮坏了。 */
export function renderCacheCleanHints(reports) {
  const map = reports || {};
  const imaging = (state.bootstrap && state.bootstrap.settings
    && state.bootstrap.settings.imaging) || {};
  const limitMb = {
    uploads: Number(imaging.auto_clean_limit_mb ?? 256),
    generated: Number(imaging.generated_clean_limit_mb ?? 512),
  };
  [['uploads', '#uploadsCacheHint'], ['generated', '#generatedCacheHint']].forEach(([scope, selector]) => {
    const el = $(selector);
    if (!el) return;
    const report = map[scope] || {};
    const parts = [];
    const referenced = Number(report.referenced_bytes || 0);
    // 被引用部分超过该目录阈值：自动清理永远到不了目标，如实说清并给出出路。
    if (referenced > 0 && referenced > Number(limitMb[scope] || 0) * 1024 * 1024) {
      parts.push(`其中 ${formatBytes(referenced)} 被历史消息引用，自动清理无法释放；需删除对应会话才能腾出`);
    }
    if (report.unreachable) parts.push('清理后仍超过阈值（可释放的都已清理）');
    if (report.error) parts.push(`上次清理失败：${report.error}`);
    el.textContent = parts.length ? `（${parts.join('；')}）` : '';
  });
}

export async function refreshImageCacheSize() {
  try {
    const result = await api('/api/imaging/stats');
    renderCacheSizes(result);
  } catch (_) { /* 打开设置页时统计失败不打扰用户 */ }
}

/* 手动清理缓存：`scope` 为 'uploads' / 'generated'（空串 = 两者都清，兼容旧调用）。
   两个按钮各自传自己的 scope，互不牵连。 */
export async function cleanImageCache(scope = '') {
  const btn = scope === 'generated' ? $('#cleanGeneratedCache') : $('#cleanUploadsCache');
  if (!btn) return;
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = '清理中…';
  try {
    const result = await api('/api/imaging/clean', { method: 'POST', body: { scope } });
    if (result.busy) {
      // 后台清理正占着进程内锁：如实说"稍后再试"，绝不显示误导性的完成提示。
      toast(result.message || '正在清理，请稍后再试');
      return;
    }
    await refreshImageCacheSize();
    // 如实报账：删了多少 / 释放多少 / **保留了多少仍在用的**。
    // 后端会保护被消息、快照、聊天背景图引用的文件，所以"点了清理却没删多少"是正常结果，
    // 必须说明原因——否则会被当成按钮坏了（用户报障：清缓存把自己设的背景图清没了）。
    const removed = Number(result.removed || 0);
    const freed = Number(result.freed || 0);
    const keptReferenced = Number(result.skipped_referenced || 0);
    const keptRecent = Number(result.skipped_recent || 0);
    if (removed === 0) {
      const reasons = [];
      if (keptReferenced) reasons.push(`${keptReferenced} 个仍在使用（背景图 / 历史消息引用）`);
      if (keptRecent) reasons.push(`${keptRecent} 个是最近刚用的`);
      toast(reasons.length
        ? `没有可清理的缓存文件：${reasons.join('、')}，已自动保留`
        : '没有可清理的缓存文件');
    } else {
      const kept = [];
      if (keptReferenced) kept.push(`${keptReferenced} 个仍在用的文件`);
      if (keptRecent) kept.push(`${keptRecent} 个最近用过的文件`);
      toast(`已清理 ${formatBytes(freed)}（删除 ${removed} 个文件）`
        + (kept.length ? `，另保留 ${kept.join('、')}` : ''));
    }
  } catch (error) {
    toast(`清理失败：${error.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

/* ---------- 用量统计 · 分析视图 ---------- */
// token 千分位；0 归一为 "0"。
function usageNumber(value) {
  // tokens 人类可读：K/M/B 三档英文缩写（1 位小数去尾零），<1000 原样；
  // 表格单元格用 usageNumCell() 的 title 保留千分位精确值，悬停可查。
  const n = Number(value || 0);
  const abs = Math.abs(n);
  const compact = (scaled, unit) => `${scaled.toFixed(1).replace(/\.0$/, '')}${unit}`;
  if (abs >= 1e9) return compact(n / 1e9, 'B');
  if (abs >= 1e6) return compact(n / 1e6, 'M');
  if (abs >= 1e3) return compact(n / 1e3, 'K');
  return n.toLocaleString();
}

function usageNumCell(value) {
  const n = Number(value || 0);
  return `<td title="${n.toLocaleString()}">${usageNumber(n)}</td>`;
}

// 费用展示：小金额保留更多位（百万 token 级的单价常产出 0.000x），大金额收敛到 4 位。
function usageCostLabel(cost) {
  if (!cost || typeof cost !== 'object') return '';
  const amount = Number(cost.amount || 0);
  const text = amount > 0 && amount < 0.01 ? amount.toFixed(6).replace(/0+$/, '').replace(/\.$/, '') : amount.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
  return `${cost.currency || '¥'}${text}`;
}

// 多币种合计（KPI 卡 / 明细表 / 图表 tooltip 共用）：同一数组里按币种分行，不混加。
function usageCostsLabel(costs) {
  if (!Array.isArray(costs) || !costs.length) return '';
  return costs.map((item) => usageCostLabel(item)).join(' + ');
}

// 图表画费用时的折算口径：多币种不混加，但一张图只有一个 y 轴——$ 按 7.2 折算
// 进柱高，tooltip 与明细表仍按币种分列原值。汇率是展示近似值，不参与台账。
const USAGE_USD_TO_CNY = 7.2;
// 系列色板（亮暗主题都保持可读的中饱和色）：模型维度按排名取色；供应商维度
// 优先按 provider_id 映射（老朋友固定色），陌生的 id 哈希取色兜底。
const USAGE_SERIES_COLORS = ['#12A594', '#E8930C', '#3E8FE0', '#DC4A8C', '#5AA66F', '#8A93B2', '#C77E3A', '#7C5CFF'];
const USAGE_PROVIDER_COLORS = { te: '#7C5CFF', moda: '#12A594', deepseek: '#3E8FE0', mimo: '#8A93B2', openai: '#E8930C' };
const USAGE_OTHER_COLOR = '#C3C8D9';
// 分布图 / 趋势图的系列数上限：超出折成「其他」，图例不至于爆掉。
const USAGE_TOP_SERIES = 5;
// 桑基右列（模型节点）上限：再多就挤成一团，失去分流图的意义。
const USAGE_SANKEY_MAX_MODELS = 9;

// 四项默认偏好与后端 settings.usage_dash 的枚举口径一一对应（config.py
// USAGE_DASH_PREF_*）；自定义时间窗是会话态，永不落库。
const USAGE_PREF_DEFAULTS = { range: '1', gran: 'hour', chart: 'bar', metric: 'cost' };
const USAGE_PREF_KEYS = {
  range: ['today', '1', '7', '14', '29'],
  gran: ['hour', 'day'],
  chart: ['bar', 'area'],
  metric: ['cost', 'tokens'],
};

// 消耗划分维度：'model'（一序列一模型）| 'provider'（一序列一 API 供应商）。
let usageDim = 'model';
// 四项偏好（服务端 settings.usage_dash 的镜像；usageSyncPrefs 负责覆盖）。
let usagePrefs = { ...USAGE_PREF_DEFAULTS };
// 会话态窗口：range/custom 来自工具行与筛选弹窗；custom 为 null 表示快选。
let usageCustom = null;
// 图例隐藏集：跨维度保留（旧 key 在新系列里不命中即自然失效）。
let usageHidden = new Set();
// 最近一次 /api/usage/stats 响应（窗口切换重新请求；维度/图例切换纯重绘）。
let usageStatsCache = null;
// 服务端偏好是否已同步进 usagePrefs（每次进入用量页重同步 = 回到保存的默认视图）。
let usagePrefsSynced = false;

function normalizeUsagePrefs(raw = {}) {
  const source = raw && typeof raw === 'object' ? raw : {};
  const pick = (key) => (USAGE_PREF_KEYS[key].includes(source[key]) ? source[key] : USAGE_PREF_DEFAULTS[key]);
  return { range: pick('range'), gran: pick('gran'), chart: pick('chart'), metric: pick('metric') };
}

// 进入用量页时同步服务端偏好（与「侧栏分组与排序」同款：服务端是唯一事实来源）。
function usageSyncPrefs() {
  usagePrefs = normalizeUsagePrefs(state.bootstrap?.settings?.usage_dash);
  usagePrefsSynced = true;
}

// 当前窗口的毫秒区间与标签。today / 快选 N 天走 days 参数；custom 走 since/until。
// 返回的 start 已对齐桶边界（hour 对齐整点、day 对齐当地零点）——前端桶序列以
// 它为准，服务端 since 只用来圈数据，两边差几分钟不影响归桶。
function usageWindowBounds() {
  const gran = usagePrefs.gran;
  const size = gran === 'hour' ? 3_600_000 : 86_400_000;
  const align = (ms) => {
    if (gran === 'day') {
      const d = new Date(ms);
      d.setHours(0, 0, 0, 0);
      return d.getTime();
    }
    return Math.floor(ms / size) * size;
  };
  const now = Date.now();
  if (usagePrefs.range === 'custom' && usageCustom) {
    return { start: align(usageCustom.startMs), end: usageCustom.endMs, label: '自定义' };
  }
  if (usagePrefs.range === 'today') {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return { start: d.getTime(), end: now, label: '今天' };
  }
  const days = Number(usagePrefs.range) || 1;
  return { start: align(now - days * 86_400_000), end: now, label: `最近 ${days} 天` };
}

// 连续桶序列：分析图的时间轴必须补齐没有数据的桶（否则图形稀疏难读）。
// 返回 [{ key, short, full }]，key 与后端 strftime 桶键逐字对齐。
function usageBucketSeries(start, end) {
  const hour = usagePrefs.gran === 'hour';
  const size = hour ? 3_600_000 : 86_400_000;
  const p2 = (n) => String(n).padStart(2, '0');
  const buckets = [];
  for (let t = start; t < end; t += size) {
    const d = new Date(t);
    const day = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
    if (hour) {
      const key = `${day} ${p2(d.getHours())}:00`;
      buckets.push({ key, short: `${p2(d.getHours())}:00`, full: `${d.getMonth() + 1}月${d.getDate()}日 ${p2(d.getHours())}:00` });
    } else {
      buckets.push({ key: day, short: `${p2(d.getMonth() + 1)}-${p2(d.getDate())}`, full: `${d.getMonth() + 1}月${d.getDate()}日` });
    }
  }
  return buckets;
}

// 把「桶×模型」行（cache.by_bucket）聚合成图表数据：
// buckets（桶→系列→cell）+ series（窗口总量 Top N + 其他）。维度切换纯前端重算。
function usageAggregate() {
  const { start, end } = usageWindowBounds();
  const buckets = usageBucketSeries(start, end);
  const byKey = new Map(buckets.map((b) => [b.key, b]));
  const byProvider = usageDim === 'provider';
  // 窗口内各系列的 token 总量（排 Top N 用）。
  const totals = new Map();
  const cells = new Map(); // 桶键 → Map(系列 key → cell)
  for (const row of Array.isArray(usageStatsCache?.by_bucket) ? usageStatsCache.by_bucket : []) {
    if (!byKey.has(row.day)) continue;
    const key = String(row.model_key || '');
    const seriesKey = byProvider ? key.split(':', 2)[1] || key || '?' : key;
    const tokens = Number(row.total_tokens || 0);
    totals.set(seriesKey, (totals.get(seriesKey) || 0) + tokens);
    if (!cells.has(row.day)) cells.set(row.day, new Map());
    const bucketCells = cells.get(row.day);
    const cell = bucketCells.get(seriesKey) || { turns: 0, requests: 0, input_tokens: 0, cached_tokens: 0, output_tokens: 0, costs: {} };
    for (const name of ('turns requests input_tokens cached_tokens output_tokens').split(' ')) {
      cell[name] += Number(row[name] || 0);
    }
    if (row.cost && typeof row.cost === 'object' && Number(row.cost.amount)) {
      const cur = row.cost.currency || '¥';
      cell.costs[cur] = (cell.costs[cur] || 0) + Number(row.cost.amount);
    }
    bucketCells.set(seriesKey, cell);
  }
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]);
  const topKeys = new Set(ranked.slice(0, USAGE_TOP_SERIES).map(([k]) => k));
  const nameOf = (key) => {
    if (byProvider) {
      const row = (usageStatsCache?.by_bucket || []).find((r) => String(r.model_key || '').split(':', 2)[1] === key);
      return row?.provider_name || key;
    }
    const row = (usageStatsCache?.by_bucket || []).find((r) => String(r.model_key || '') === key);
    return row?.model_name || key;
  };
  const colorOf = (key, index) => {
    if (byProvider) return USAGE_PROVIDER_COLORS[key] || USAGE_SERIES_COLORS[index % USAGE_SERIES_COLORS.length];
    return USAGE_SERIES_COLORS[index % USAGE_SERIES_COLORS.length];
  };
  const series = ranked
    .filter(([k]) => topKeys.has(k))
    .map(([k], index) => ({ key: k, name: nameOf(k), color: colorOf(k, index) }));
  if (ranked.length > USAGE_TOP_SERIES) series.push({ key: '__other__', name: '其他', color: USAGE_OTHER_COLOR });
  // hidden 集里的旧维度 key 不影响新系列；渲染时 off 态按 key 命中。
  return { buckets, cells, series, start, end };
}

function usageFoldCell(cell) {
  if (!cell) return null;
  return {
    ...cell,
    total_tokens: cell.input_tokens + cell.output_tokens,
  };
}

// 桶内折算费用（画图口径：$ 折 ¥，见 USAGE_USD_TO_CNY 注释）。
function usageCellCostValue(costs) {
  let sum = 0;
  for (const [currency, amount] of Object.entries(costs || {})) {
    sum += Number(amount || 0) * (currency === '$' ? USAGE_USD_TO_CNY : 1);
  }
  return sum;
}

// 当前主请求：today/快选走 days，自定义走 since/until（成对，服务端校验）。
export async function loadUsageStats() {
  if (!$('#usageKpiCalls')) return;
  if (!usagePrefsSynced) usageSyncPrefs();
  const { start, end } = usageWindowBounds();
  try {
    let stats;
    if (usagePrefs.range === 'custom' && usageCustom) {
      stats = await api(`/api/usage/stats?since=${encodeURIComponent(usageCustom.startMs)}&until=${encodeURIComponent(usageCustom.endMs)}&bucket=${usagePrefs.gran}`);
    } else if (usagePrefs.range === 'today') {
      // 今天 = 服务端按 days=1 圈数据，前端桶序列只收零点之后的桶。
      stats = await api(`/api/usage/stats?days=1&bucket=${usagePrefs.gran}`);
    } else {
      stats = await api(`/api/usage/stats?days=${Number(usagePrefs.range) || 1}&bucket=${usagePrefs.gran}`);
    }
    usageStatsCache = stats;
    usageRenderAll({ start, end });
  } catch (error) {
    toast(`用量统计加载失败：${error.message}`);
  }
}

/* ═══════════ 手绘 SVG 图表（零依赖；颜色消费语义变量，dark 自动适配） ═══════════ */
const USAGE_SVG_NS = 'http://www.w3.org/2000/svg';

function usageSvgEl(tag, attrs = {}) {
  const node = document.createElementNS(USAGE_SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

// Catmull-Rom → Bezier 平滑折线（预览同款算法）。
function usageSmoothPath(pts) {
  if (pts.length < 2) return '';
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[Math.min(pts.length - 1, i + 2)];
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += `C${c1[0]},${c1[1]} ${c2[0]},${c2[1]} ${p2[0]},${p2[1]}`;
  }
  return d;
}

// tooltip 节点：必须挂在模态 dialog（top layer）内部——body 上的 fixed 浮层会被
// 设置弹窗盖住（index.html 快捷提示词面板同款教训）。惰性创建，只挂一次。
function usageTooltipEl() {
  const host = $('#settingsDialog') || document.body;
  let tip = host.querySelector('.usage-tooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'usage-tooltip';
    host.appendChild(tip);
  }
  return tip;
}

function usageShowTooltip(html, x, y) {
  const tip = usageTooltipEl();
  tip.innerHTML = html;
  tip.style.display = 'block';
  const rect = tip.getBoundingClientRect();
  // 防出屏：右缘放不下翻到指针左侧；下缘放不下翻上去。用 innerHeight/innerWidth
  // 兜底（tooltip 是短命的悬浮物，不需要按可视视口精确约束）。
  let left = x + 14;
  let top = y + 12;
  if (left + rect.width > window.innerWidth - 8) left = x - rect.width - 12;
  if (top + rect.height > window.innerHeight - 8) top = y - rect.height - 10;
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}

function usageHideTooltip() {
  const tip = ($('#settingsDialog') || document.body).querySelector('.usage-tooltip');
  if (tip) tip.style.display = 'none';
}

function usageFmtTok(n) {
  n = Number(n || 0);
  if (n >= 1e8) return `${(n / 1e8).toFixed(2)} 亿`;
  if (n >= 1e4) return `${(n / 1e4).toFixed(1)} 万`;
  return usageNumber(n);
}

function usageFmtCosts(costs, { short = false } = {}) {
  const parts = Object.entries(costs || {})
    .filter(([, v]) => Number(v) > 0.0001)
    .map(([c, v]) => `${c}${v.toFixed(short ? 0 : 2)}`);
  return parts.length ? parts.join(' + ') : '—';
}

function usageRenderLegend(containerId, series) {
  const box = $(`#${containerId}`);
  if (!box) return;
  box.innerHTML = '';
  for (const s of series) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = `usage-legend-item${usageHidden.has(s.key) ? ' off' : ''}`;
    item.title = '点击显示 / 隐藏该系列';
    item.innerHTML = `<span class="usage-legend-dot" style="background:${s.color}"></span>${escapeHtml(s.name)}`;
    item.addEventListener('click', () => {
      if (usageHidden.has(s.key)) usageHidden.delete(s.key);
      else usageHidden.add(s.key);
      usageRenderAll();
    });
    box.appendChild(item);
  }
}

// 空窗口占位：没有数据的图不画空轴，给一行居中说明。
function usageEmptySvg(width, height, text) {
  const svg = usageSvgEl('svg', { viewBox: `0 0 ${width} ${height}` });
  const t = usageSvgEl('text', { x: width / 2, y: height / 2, 'text-anchor': 'middle', 'font-size': 13, fill: 'var(--muted)' });
  t.textContent = text;
  svg.appendChild(t);
  return svg;
}

/* 消耗分布：堆叠柱状 / 半透明面积（指标可切 花费 | Token）。 */
function usageRenderDist(agg) {
  const box = $('#usageDistBox');
  if (!box) return;
  box.innerHTML = '';
  const W = 1000;
  const H = 260;
  const padL = 46;
  const padR = 10;
  const padT = 12;
  const padB = 26;
  const iw = W - padL - padR;
  const ih = H - padT - padB;
  const { buckets, cells, series } = agg;
  const metric = usagePrefs.metric;
  const visible = series.filter((s) => !usageHidden.has(s.key));
  const valueOf = (cell) => {
    if (!cell) return 0;
    if (metric === 'tokens') return cell.input_tokens + cell.output_tokens;
    return usageCellCostValue(cell.costs);
  };
  const bucketTotal = (bucket) => {
    const bucketCells = cells.get(bucket.key);
    if (!bucketCells) return 0;
    let sum = 0;
    for (const cell of bucketCells.values()) sum += valueOf(cell);
    return sum;
  };
  const maxV = Math.max(1e-9, ...buckets.map(bucketTotal));
  const svg = usageSvgEl('svg', { viewBox: `0 0 ${W} ${H}` });
  for (let i = 0; i <= 4; i++) {
    const v = (maxV * i) / 4;
    const y = padT + ih - (ih * i) / 4;
    svg.appendChild(usageSvgEl('line', { x1: padL, y1: y, x2: W - padR, y2: y, stroke: 'var(--line)', 'stroke-width': 1 }));
    const t = usageSvgEl('text', { x: padL - 7, y: y + 4, 'text-anchor': 'end', 'font-size': 10.5, fill: 'var(--muted)' });
    t.textContent = metric === 'tokens' ? usageFmtTok(v) : `¥${v.toFixed(v >= 100 ? 0 : v >= 1 ? 1 : 2)}`;
    svg.appendChild(t);
  }
  const n = buckets.length;
  const step = iw / Math.max(1, n);
  const barW = Math.max(2, Math.min(26, step * 0.62));
  const labelEvery = Math.max(1, Math.ceil(n / 12));
  buckets.forEach((bucket, i) => {
    const cx = padL + step * (i + 0.5);
    if (i % labelEvery === 0) {
      const t = usageSvgEl('text', { x: cx, y: H - 8, 'text-anchor': 'middle', 'font-size': 10, fill: 'var(--muted)' });
      t.textContent = bucket.short;
      svg.appendChild(t);
    }
    if (usagePrefs.chart === 'bar') {
      const bucketCells = cells.get(bucket.key);
      if (!bucketCells) return;
      let acc = 0;
      for (const s of visible) {
        const v = valueOf(bucketCells.get(s.key));
        if (!v) continue;
        const hVal = (v / maxV) * ih;
        svg.appendChild(usageSvgEl('rect', {
          x: cx - barW / 2, y: padT + ih - acc - hVal, width: barW, height: Math.max(hVal, 0.5),
          fill: s.color, rx: Math.min(2.5, barW / 4), opacity: 0.92,
        }));
        acc += hVal;
      }
    }
  });
  if (usagePrefs.chart === 'area') {
    // 面积图：系列间独立归一、半透明重叠（不堆叠），贴近「趋势叠加」的读法。
    for (const s of [...visible].reverse()) {
      const pts = buckets.map((bucket, i) => {
        const v = valueOf(cells.get(bucket.key)?.get(s.key)) / maxV;
        return [padL + step * (i + 0.5), padT + ih - v * ih];
      });
      const line = usageSmoothPath(pts);
      if (!line) continue;
      svg.appendChild(usageSvgEl('path', { d: `${line}L${pts[pts.length - 1][0]},${padT + ih}L${pts[0][0]},${padT + ih}Z`, fill: s.color, opacity: 0.16 }));
      svg.appendChild(usageSvgEl('path', { d: line, fill: 'none', stroke: s.color, 'stroke-width': 1.8 }));
    }
  }
  // 按桶的 hover 命中区（透明矩形；触摸设备跟随手指位置出 tooltip）。
  buckets.forEach((bucket, i) => {
    const hit = usageSvgEl('rect', { x: padL + step * i, y: padT, width: step, height: ih, fill: 'transparent' });
    hit.addEventListener('mousemove', (ev) => {
      const bucketCells = cells.get(bucket.key);
      const rows = visible.map((s) => {
        const cell = bucketCells?.get(s.key);
        if (!cell) return '';
        const val = metric === 'tokens' ? `${usageFmtTok(cell.input_tokens + cell.output_tokens)} tok` : usageFmtCosts(cell.costs);
        return `<div class="usage-tooltip-row"><span class="usage-legend-dot" style="background:${s.color}"></span><span class="usage-tt-name">${escapeHtml(s.name)}</span><span class="usage-tt-val">${val}</span></div>`;
      }).join('');
      const tot = bucketTotal(bucket);
      const totLabel = metric === 'tokens' ? `${usageFmtTok(tot)} token` : `¥${tot.toFixed(2)}`;
      usageShowTooltip(`<div class="usage-tooltip-title">${escapeHtml(bucket.full)}</div>${rows}<div class="usage-tooltip-muted">合计 ${totLabel}</div>`, ev.clientX, ev.clientY);
    });
    hit.addEventListener('mouseleave', usageHideTooltip);
    svg.appendChild(hit);
  });
  if (!visible.length || !n) {
    box.innerHTML = '';
    box.appendChild(usageEmptySvg(W, H, '暂无数据——开始对话后这里会出现图表。'));
  } else {
    box.appendChild(svg);
  }
  usageRenderLegend('usageDistLegend', series);
  const windowSum = buckets.reduce((s, b) => s + bucketTotal(b), 0);
  const sumEl = $('#usageDistSum');
  if (sumEl) sumEl.textContent = metric === 'tokens' ? `合计 ${usageFmtTok(windowSum)} token` : `合计 ¥${windowSum.toFixed(2)}（$ 按 ${USAGE_USD_TO_CNY} 折算画图，明细仍分币种）`;
}

/* 调用趋势：按系列的多序列平滑线（请求次数）。 */
function usageRenderTrend(agg) {
  const box = $('#usageTrendBox');
  if (!box) return;
  box.innerHTML = '';
  const W = 1000;
  const H = 230;
  const padL = 40;
  const padR = 10;
  const padT = 12;
  const padB = 24;
  const iw = W - padL - padR;
  const ih = H - padT - padB;
  const { buckets, cells, series } = agg;
  const visible = series.filter((s) => !usageHidden.has(s.key));
  const requestsOf = (bucket, key) => cells.get(bucket.key)?.get(key)?.requests || 0;
  const maxV = Math.max(1, ...buckets.map((b) => Math.max(0, ...visible.map((s) => requestsOf(b, s.key)))));
  const svg = usageSvgEl('svg', { viewBox: `0 0 ${W} ${H}` });
  for (let i = 0; i <= 3; i++) {
    const y = padT + ih - (ih * i) / 3;
    svg.appendChild(usageSvgEl('line', { x1: padL, y1: y, x2: W - padR, y2: y, stroke: 'var(--line)', 'stroke-width': 1 }));
    const t = usageSvgEl('text', { x: padL - 6, y: y + 4, 'text-anchor': 'end', 'font-size': 10.5, fill: 'var(--muted)' });
    t.textContent = String(Math.round((maxV * i) / 3));
    svg.appendChild(t);
  }
  const n = buckets.length;
  const step = iw / Math.max(1, n);
  const labelEvery = Math.max(1, Math.ceil(n / 12));
  buckets.forEach((b, i) => {
    if (i % labelEvery === 0) {
      const t = usageSvgEl('text', { x: padL + step * (i + 0.5), y: H - 6, 'text-anchor': 'middle', 'font-size': 10, fill: 'var(--muted)' });
      t.textContent = b.short;
      svg.appendChild(t);
    }
  });
  for (const s of visible) {
    const pts = buckets.map((b, i) => [padL + step * (i + 0.5), padT + ih - (requestsOf(b, s.key) / maxV) * ih]);
    const line = usageSmoothPath(pts);
    if (!line) continue;
    svg.appendChild(usageSvgEl('path', { d: `${line}L${pts[pts.length - 1][0]},${padT + ih}L${pts[0][0]},${padT + ih}Z`, fill: s.color, opacity: 0.08 }));
    svg.appendChild(usageSvgEl('path', { d: line, fill: 'none', stroke: s.color, 'stroke-width': 1.8 }));
  }
  buckets.forEach((bucket, i) => {
    const hit = usageSvgEl('rect', { x: padL + step * i, y: padT, width: step, height: ih, fill: 'transparent' });
    hit.addEventListener('mousemove', (ev) => {
      const rows = visible.map((s) => {
        const cell = cells.get(bucket.key)?.get(s.key);
        return `<div class="usage-tooltip-row"><span class="usage-legend-dot" style="background:${s.color}"></span><span class="usage-tt-name">${escapeHtml(s.name)}</span><span class="usage-tt-val">${usageNumber(cell?.requests || 0)} 次 · ${usageNumber(cell?.turns || 0)} 轮</span></div>`;
      }).join('');
      usageShowTooltip(`<div class="usage-tooltip-title">${escapeHtml(bucket.full)}</div>${rows}`, ev.clientX, ev.clientY);
    });
    hit.addEventListener('mouseleave', usageHideTooltip);
    svg.appendChild(hit);
  });
  if (!visible.length || !n) {
    box.innerHTML = '';
    box.appendChild(usageEmptySvg(W, H, '暂无数据。'));
  } else {
    box.appendChild(svg);
  }
  usageRenderLegend('usageTrendLegend', series);
  const calls = buckets.reduce((s, b) => s + [...(cells.get(b.key)?.values() || [])].reduce((s2, c) => s2 + c.requests, 0), 0);
  const sumEl = $('#usageTrendSum');
  if (sumEl) sumEl.textContent = `窗口内共 ${usageNumber(calls)} 次请求`;
}

/* API 供应商对比：横向条（多供应商是我们区别于单 API 后台的根本差异）。 */
function usageRenderProviders() {
  const box = $('#usageProvBox');
  if (!box) return;
  box.innerHTML = '';
  const rows = Array.isArray(usageStatsCache?.by_provider) ? usageStatsCache.by_provider : [];
  const W = 1000;
  const rowH = 40;
  const padL = 130;
  const padR = 160;
  if (!rows.length) {
    box.appendChild(usageEmptySvg(W, 80, '暂无数据。'));
    return;
  }
  const cnyOf = (row) => {
    let sum = 0;
    for (const item of row.costs || []) sum += Number(item.amount || 0) * (item.currency === '$' ? USAGE_USD_TO_CNY : 1);
    return sum;
  };
  const sorted = [...rows].sort((a, b) => cnyOf(b) - cnyOf(a));
  const maxCny = Math.max(1e-9, ...sorted.map(cnyOf));
  const H = sorted.length * rowH + 8;
  const svg = usageSvgEl('svg', { viewBox: `0 0 ${W} ${H}` });
  sorted.forEach((row, i) => {
    const y = i * rowH + 6;
    const providerId = String(row.provider_id || '');
    const color = USAGE_PROVIDER_COLORS[providerId] || USAGE_SERIES_COLORS[i % USAGE_SERIES_COLORS.length];
    const name = usageSvgEl('text', { x: padL - 10, y: y + 15, 'text-anchor': 'end', 'font-size': 12, fill: 'var(--text)' });
    name.textContent = `${row.provider_name || providerId || '未知供应商'}${row.priced ? '' : ' ·'}`;
    svg.appendChild(name);
    const bw = (W - padL - padR) * (cnyOf(row) / maxCny);
    svg.appendChild(usageSvgEl('rect', { x: padL, y: y + 4, width: Math.max(bw, 2), height: 16, rx: 4, fill: color, opacity: 0.85 }));
    const val = usageSvgEl('text', { x: padL + Math.max(bw, 2) + 8, y: y + 16, 'font-size': 11.5, fill: 'var(--text)', 'font-variant-numeric': 'tabular-nums' });
    val.textContent = `${usageCostsLabel(row.costs) || '未定价'} · ${usageFmtTok(row.total_tokens)} tok · ${usageNumber(row.requests)} 次`;
    svg.appendChild(val);
  });
  box.appendChild(svg);
  const unpriced = sorted.filter((row) => !row.priced);
  const hint = $('#usageUnpricedHint');
  if (hint) {
    hint.hidden = unpriced.length === 0;
    hint.textContent = unpriced.length
      ? `${usageNumber(Number(usageStatsCache?.totals?.unpriced_turns || 0))} 轮来自未设单价的供应商（${unpriced.map((row) => row.provider_name).join('、')}），未计入费用；点右上角「费用单价」补设后即可追溯。`
      : '';
  }
}

/* 缓存命中率：按当前维度的系列横向条（cached / input）。 */
function usageRenderCache(agg) {
  const box = $('#usageCacheBox');
  if (!box) return;
  box.innerHTML = '';
  const W = 1000;
  const rowH = 34;
  const padL = 150;
  const padR = 100;
  const byProvider = usageDim === 'provider';
  const acc = new Map();
  for (const row of Array.isArray(usageStatsCache?.by_bucket) ? usageStatsCache.by_bucket : []) {
    const key = String(row.model_key || '');
    const seriesKey = byProvider ? key.split(':', 2)[1] || key || '?' : key;
    const name = byProvider ? (row.provider_name || seriesKey) : (row.model_name || key);
    const item = acc.get(seriesKey) || { name, input_tokens: 0, cached_tokens: 0 };
    item.input_tokens += Number(row.input_tokens || 0);
    item.cached_tokens += Number(row.cached_tokens || 0);
    acc.set(seriesKey, item);
  }
  const rows = [...acc.values()]
    .map((item) => ({ ...item, rate: item.input_tokens ? item.cached_tokens / item.input_tokens : 0 }))
    .filter((item) => item.input_tokens > 0)
    .sort((a, b) => b.rate - a.rate);
  if (!rows.length) {
    box.appendChild(usageEmptySvg(W, 80, '暂无数据。'));
    return;
  }
  const H = rows.length * rowH + 8;
  const svg = usageSvgEl('svg', { viewBox: `0 0 ${W} ${H}` });
  const bw = W - padL - padR;
  rows.forEach((row, i) => {
    const y = i * rowH + 5;
    const name = usageSvgEl('text', { x: padL - 10, y: y + 14, 'text-anchor': 'end', 'font-size': 12, fill: 'var(--text)' });
    name.textContent = row.name;
    svg.appendChild(name);
    svg.appendChild(usageSvgEl('rect', { x: padL, y: y + 3, width: bw, height: 13, rx: 4, fill: 'var(--surface-2)' }));
    svg.appendChild(usageSvgEl('rect', { x: padL, y: y + 3, width: Math.max(bw * row.rate, 1), height: 13, rx: 4, fill: 'var(--success)', opacity: 0.8 }));
    const val = usageSvgEl('text', { x: padL + bw + 8, y: y + 14, 'font-size': 11.5, fill: 'var(--muted)', 'font-variant-numeric': 'tabular-nums' });
    val.textContent = `${(row.rate * 100).toFixed(1)}%（缓存 ${usageFmtTok(row.cached_tokens)}）`;
    svg.appendChild(val);
  });
  box.appendChild(svg);
}

/* 分流：供应商 → 模型 两层桑基（按 Token 量；hover 看单流占比）。 */
function usageRenderSankey() {
  const box = $('#usageSankeyBox');
  if (!box) return;
  box.innerHTML = '';
  const modelRows = Array.isArray(usageStatsCache?.by_model) ? usageStatsCache.by_model : [];
  if (!modelRows.length) {
    box.appendChild(usageEmptySvg(1000, 160, '暂无数据。'));
    return;
  }
  const flows = new Map(); // providerId → Map(modelKey → tokens)
  const provTot = new Map();
  const modelTot = new Map();
  const meta = new Map(); // modelKey → { name, providerId }
  for (const row of modelRows) {
    const key = String(row.model_key || '');
    const providerId = key.split(':', 2)[1] || key || '?';
    const tokens = Number(row.total_tokens || 0);
    if (!flows.has(providerId)) flows.set(providerId, new Map());
    flows.get(providerId).set(key, (flows.get(providerId).get(key) || 0) + tokens);
    provTot.set(providerId, (provTot.get(providerId) || 0) + tokens);
    modelTot.set(key, (modelTot.get(key) || 0) + tokens);
    meta.set(key, { name: row.model_name || key, providerId });
  }
  const provs = [...provTot.entries()].sort((a, b) => b[1] - a[1]);
  const models = [...modelTot.entries()].sort((a, b) => b[1] - a[1]).slice(0, USAGE_SANKEY_MAX_MODELS);
  const grand = [...provTot.values()].reduce((s, v) => s + v, 0) || 1;
  const W = 1000;
  const H = 300;
  const padL = 96;
  const padR = 110;
  const padT = 14;
  const padB = 14;
  const usableH = H - padT - padB;
  const gap = 14;
  const provHsum = usableH - gap * (provs.length - 1);
  const modelHsum = usableH - gap * (models.length - 1);
  const x0 = padL;
  const x1 = W - padR;
  const nodeW = 13;
  const svg = usageSvgEl('svg', { viewBox: `0 0 ${W} ${H}` });
  const provNameOf = (providerId) => {
    for (const row of modelRows) {
      if (String(row.model_key || '').split(':', 2)[1] === providerId) return row.provider_name || providerId;
    }
    return providerId;
  };
  const colorOf = (providerId) => USAGE_PROVIDER_COLORS[providerId] || USAGE_SERIES_COLORS[provs.findIndex(([pid]) => pid === providerId) % USAGE_SERIES_COLORS.length];
  let y = padT;
  const provGeo = new Map();
  for (const [providerId, tokens] of provs) {
    const h = Math.max(10, (provHsum * tokens) / grand);
    provGeo.set(providerId, { y0: y, h, tokens });
    svg.appendChild(usageSvgEl('rect', { x: x0 - nodeW, y, width: nodeW, height: h, rx: 3, fill: colorOf(providerId) }));
    const lbl = usageSvgEl('text', { x: x0 - nodeW - 7, y: y + h / 2 + 4, 'text-anchor': 'end', 'font-size': 11.5, fill: 'var(--text)' });
    lbl.textContent = provNameOf(providerId);
    svg.appendChild(lbl);
    const val = usageSvgEl('text', { x: x0 - nodeW - 7, y: y + h / 2 + 16, 'text-anchor': 'end', 'font-size': 10, fill: 'var(--muted)' });
    val.textContent = usageFmtTok(tokens);
    svg.appendChild(val);
    y += h + gap;
  }
  y = padT;
  const modelGeo = new Map();
  for (const [modelKey, tokens] of models) {
    const info = meta.get(modelKey);
    const h = Math.max(8, (modelHsum * tokens) / grand);
    modelGeo.set(modelKey, { y0: y, h, tokens });
    svg.appendChild(usageSvgEl('rect', { x: x1, y, width: nodeW, height: h, rx: 3, fill: colorOf(info?.providerId || '') }));
    const lbl = usageSvgEl('text', { x: x1 + nodeW + 7, y: y + h / 2 + 4, 'font-size': 11.5, fill: 'var(--text)' });
    lbl.textContent = info?.name || modelKey;
    svg.appendChild(lbl);
    y += h + gap;
  }
  // 流带：按目标模型内堆叠；hover 高亮 + 显示占比。
  const cursorAt = new Map();
  for (const [providerId] of provs) {
    const g0 = provGeo.get(providerId);
    let srcOffset = 0;
    const targets = models
      .map(([mk]) => [mk, flows.get(providerId)?.get(mk) || 0])
      .filter(([, tokens]) => tokens > 0)
      .sort((a, b) => b[1] - a[1]);
    for (const [modelKey, tokens] of targets) {
      const g1 = modelGeo.get(modelKey);
      const h0 = Math.max(2, (g0.h * tokens) / g0.tokens);
      const yOff = cursorAt.get(modelKey) || 0;
      const h1 = Math.max(2, (g1.h * tokens) / g1.tokens);
      const y0 = g0.y0 + srcOffset;
      const y1 = g1.y0 + yOff;
      const mx = (x0 + x1) / 2;
      const path = usageSvgEl('path', {
        d: `M${x0},${y0}C${mx},${y0} ${mx},${y1} ${x1},${y1}L${x1},${y1 + h1}C${mx},${y1 + h1} ${mx},${y0 + h0} ${x0},${y0 + h0}Z`,
        fill: colorOf(providerId), opacity: 0.3, class: 'usage-sankey-ribbon',
      });
      path.addEventListener('mousemove', (ev) => {
        path.setAttribute('opacity', '0.55');
        usageShowTooltip(`<div class="usage-tooltip-title">${escapeHtml(provNameOf(providerId))} → ${escapeHtml(meta.get(modelKey)?.name || modelKey)}</div><div class="usage-tooltip-row"><span class="usage-tt-name">Token</span><span class="usage-tt-val">${usageFmtTok(tokens)}</span></div><div class="usage-tooltip-row"><span class="usage-tt-name">占比</span><span class="usage-tt-val">${((tokens / grand) * 100).toFixed(1)}%</span></div><div class="usage-tooltip-muted">窗口内按 Token 量</div>`, ev.clientX, ev.clientY);
      });
      path.addEventListener('mouseleave', () => {
        path.setAttribute('opacity', '0.3');
        usageHideTooltip();
      });
      svg.appendChild(path);
      srcOffset += h0;
      cursorAt.set(modelKey, yOff + h1);
    }
  }
  box.appendChild(svg);
}

/* 明细表（沿用旧表口径；维度随工具行切换）。 */
function usageRenderTable() {
  const byProvider = usageDim === 'provider';
  const head = $('#usageDetailHead');
  const body = $('#usageDetailBody');
  const title = $('#usageDetailTitle');
  const desc = $('#usageDetailDesc');
  if (!head || !body) return;
  if (title) title.textContent = byProvider ? '按 API 供应商明细' : '按模型明细';
  if (desc) desc.textContent = byProvider
    ? '当前时间窗内每个 API 供应商的消耗与费用。'
    : '当前时间窗内每个模型的消耗与费用。';
  head.innerHTML = `<th>${byProvider ? 'API 供应商' : '模型'}</th><th>轮次</th><th>请求</th><th>输入</th><th>命中</th><th>输出</th><th>命中率</th><th>费用</th>`;
  const rows = byProvider
    ? (Array.isArray(usageStatsCache?.by_provider) ? usageStatsCache.by_provider : [])
    : (Array.isArray(usageStatsCache?.by_model) ? usageStatsCache.by_model : []);
  body.innerHTML = rows.length
    ? rows.map((row) => {
      const input = Number(row.input_tokens || 0);
      const cached = Number(row.cached_tokens || 0);
      const rate = input ? `${((cached / input) * 100).toFixed(1)}%` : '—';
      const label = byProvider
        ? (row.provider_name || row.provider_id || '未知供应商')
        : (row.model_name || row.model_key || '未知模型');
      const costCell = row.priced
        ? escapeHtml(byProvider ? (usageCostsLabel(row.costs) || '—') : usageCostLabel(row.cost))
        : '<span class="usage-unpriced-cell">未定价</span>';
      return `<tr><td>${escapeHtml(label)}</td>${usageNumCell(row.turns)}${usageNumCell(row.requests)}${usageNumCell(input)}${usageNumCell(cached)}${usageNumCell(row.output_tokens)}<td>${rate}</td><td>${costCell}</td></tr>`;
    }).join('')
    : '<tr><td colspan="8" class="usage-empty">暂无数据——开始对话后这里会出现统计。</td></tr>';
}

/* KPI 六卡（口径与表格一致：窗口聚合、多币种分行、未定价轮次单独提示）。 */
function usageRenderKpi(agg) {
  const totals = usageStatsCache?.totals || {};
  const requests = Number(totals.requests || 0);
  const input = Number(totals.input_tokens || 0);
  const cached = Number(totals.cached_tokens || 0);
  const output = Number(totals.output_tokens || 0);
  const minutes = Math.max(1, (agg.end - agg.start) / 60000);
  const setText = (id, text) => {
    const el = $(`#${id}`);
    if (el) el.textContent = text;
  };
  const callsEl = $('#usageKpiCalls');
  if (callsEl) callsEl.innerHTML = `${usageNumber(requests)}<small>次</small>`;
  setText('usageKpiCallsSub', `${usageNumber(totals.turns || 0)} 轮对话`);
  setText('usageKpiCost', usageCostsLabel(totals.costs));
  setText('usageKpiCostSub', Number(totals.unpriced_turns || 0) ? `${usageNumber(totals.unpriced_turns)} 轮未定价未计入` : '全部供应商已计价');
  const tokensEl = $('#usageKpiTokens');
  if (tokensEl) tokensEl.innerHTML = `${usageFmtTok(input + output)}<small>tok</small>`;
  setText('usageKpiTokensSub', `输入 ${usageFmtTok(input)} · 输出 ${usageFmtTok(output)}`);
  setText('usageKpiRpm', (requests / minutes).toFixed(2));
  setText('usageKpiTpm', ((input + output) / minutes / 1000).toFixed(1));
  setText('usageKpiCache', input ? `${((cached / input) * 100).toFixed(1)}%` : '—');
  setText('usageKpiCacheSub', `缓存输入 ${usageFmtTok(cached)} / 总输入 ${usageFmtTok(input)}`);
}

/* 总渲染：窗口标签 + KPI + 全部图表 + 明细表。维度/图例切换不重新请求。 */
function usageRenderAll(bounds = null) {
  if (!usageStatsCache) return;
  const { start, end, label } = bounds || usageWindowBounds();
  const rangeLabel = usagePrefs.range === 'custom' && usageCustom
    ? `${new Date(start).toLocaleDateString()} ~ ${new Date(end).toLocaleDateString()}`
    : label;
  usageSyncWindowControls();
  const agg = usageAggregate();
  usageRenderKpi(agg);
  usageRenderDist(agg);
  usageRenderTrend(agg);
  usageRenderProviders();
  usageRenderCache(agg);
  usageRenderSankey();
  usageRenderTable();
}

// 工具行控件与状态的同步（active 态 / select 值 / 自定义 chip 的高亮）。
function usageSyncWindowControls() {
  const chips = $('#usageRangeChips');
  if (chips) {
    for (const chip of chips.querySelectorAll('[data-usage-range]')) {
      chip.classList.toggle('active', chip.dataset.usageRange === usagePrefs.range);
    }
  }
  const dim = $('#usageDimSeg');
  if (dim) {
    for (const btn of dim.querySelectorAll('[data-usage-dim]')) {
      btn.classList.toggle('active', btn.dataset.usageDim === usageDim);
    }
  }
  const gran = $('#usageGranSel');
  if (gran) gran.value = usagePrefs.gran;
  const chart = $('#usageChartSeg');
  if (chart) {
    for (const btn of chart.querySelectorAll('[data-usage-chart]')) {
      btn.classList.toggle('active', btn.dataset.usageChart === usagePrefs.chart);
    }
  }
  const metric = $('#usageMetricSeg');
  if (metric) {
    for (const btn of metric.querySelectorAll('[data-usage-metric]')) {
      btn.classList.toggle('active', btn.dataset.usageMetric === usagePrefs.metric);
    }
  }
}

/* ═══════════ 工具行 / 弹窗事件（由 15-bind-events.js 绑定） ═══════════ */

export function setUsageRange(range) {
  if (range === 'custom') {
    openUsageFilterDialog();
    return;
  }
  if (!USAGE_PREF_KEYS.range.includes(range)) return;
  usagePrefs.range = range;
  usageCustom = null;
  void loadUsageStats();
}

export function setUsageDim(dim) {
  if (dim !== 'model' && dim !== 'provider') return;
  usageDim = dim;
  usageRenderAll();
}

export function setUsageGran(gran) {
  if (!USAGE_PREF_KEYS.gran.includes(gran)) return;
  usagePrefs.gran = gran;
  void loadUsageStats();
}

export function setUsageChart(type) {
  if (!USAGE_PREF_KEYS.chart.includes(type)) return;
  usagePrefs.chart = type;
  usageRenderAll();
}

export function setUsageMetric(metric) {
  if (!USAGE_PREF_KEYS.metric.includes(metric)) return;
  usagePrefs.metric = metric;
  usageRenderAll();
}

/* ---------- 费用单价弹层（用量统计 → 按 base_url 分组 → 模型） ---------- */
// 为什么单开一处：单价原先挂在「API 供应商 → 编辑」表单里，和连接配置混在一起，
// 既看不出「这是谁的价」，改 Key / 改模型时还可能顺手把价格清掉。这里按
// 「供应商 → 模型」两层管理，写入只走 POST /api/providers/pricing（只提交
// id/kind/pricing）——**不复用整表单**，因为那张表单要求 base_url/model/api_key，
// 前端手里也没有真 Key，用它提交等于顺手覆盖连接配置。
//
// 分组键 = base_url 归一（去空白、去末尾斜杠、小写）：卡片名是用户随手填的
// （「主力对话」「深度思考」都可能是同一个端点），而归一后的地址才是计费身份。
// 组内可整组套用币种；行内符号按钮只改单个模型；未定价的卡片收在「添加模型定价」里。
const PRICING_BASE_CURRENCIES = ['¥', '$', '€'];
const PRICING_TIER_FIELDS = ['input_per_million', 'cached_input_per_million', 'output_per_million'];
const PRICING_TIER_LABELS = ['输入（未命中）', '缓存命中', '输出'];
const PRICING_DEFAULT_CURRENCY = '¥';

// 一组 = 同一 base_url 归一后的所有卡片；priced = 已设单价的（含本次新加的草稿），
// pool = 还没定价、可从「添加模型定价」拉进来的。全部派生自 bootstrap 的卡片列表。
let pricingGroups = [];
let pricingCurrent = -1;
// 本次打开期间「从池子加进来、还没写库」的卡片 id（草稿行）。
let pricingDraftIds = new Set();

function pricingNormUrl(url) {
  return String(url || '').trim().toLowerCase().replace(/\/+$/, '');
}

function pricingOf(provider) {
  const pricing = provider?.pricing;
  return (pricing && typeof pricing === 'object') ? pricing : {};
}

function pricingHasValue(provider) {
  const pricing = pricingOf(provider);
  return PRICING_TIER_FIELDS.some((field) => {
    const value = pricing[field];
    return value !== undefined && value !== null && value !== '';
  });
}

function pricingCurrencyOf(provider) {
  return String(pricingOf(provider).currency || PRICING_DEFAULT_CURRENCY);
}

function pricingBuildGroups() {
  const map = new Map();
  for (const provider of providerProfiles()) {
    const key = pricingNormUrl(provider.base_url);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(provider);
  }
  pricingGroups = [...map.entries()].map(([url, cards]) => ({
    url,
    cards,
    priced: cards.filter((card) => pricingHasValue(card) || pricingDraftIds.has(card.id)),
    pool: cards.filter((card) => !pricingHasValue(card) && !pricingDraftIds.has(card.id)),
  }));
}

// 面板头角标 = 全站未定价卡片数（一眼看出还有几个模型没计价）。
export function refreshUsagePricingBadge() {
  const badge = $('#usagePricingCount');
  if (!badge) return;
  const unpriced = providerProfiles().filter((provider) => !pricingHasValue(provider)).length;
  badge.textContent = String(unpriced);
  badge.hidden = unpriced === 0;
}

// 写库：只提交 id/kind/pricing。成功后就地改 bootstrap 里的卡片（不重拉整份列表）。
async function pricingWrite(provider, pricing) {
  const result = await api('/api/providers/pricing', {
    method: 'POST',
    body: { id: provider.id, kind: provider.kind || 'online', pricing },
  });
  pricingPatchProvider(provider.id, result?.pricing);
  return result;
}

function pricingPatchProvider(id, pricing) {
  for (const key of ['providers', 'model_profiles']) {
    const list = state.bootstrap?.[key];
    if (!Array.isArray(list)) continue;
    const item = list.find((entry) => entry.id === id);
    if (!item) continue;
    if (pricing && Object.keys(pricing).length) item.pricing = pricing;
    else delete item.pricing;
  }
}

// 改完价格后：角标 → 分组 → 币种条 → 弹层 → 用量页（KPI / 明细 / 未定价提示都按当前单价现算）。
// **币种条必须在这里一起重渲染**：整组套用币种 / 移除自定义币种 / 删除定价都会改变
// 「哪些币种正在被用」，漏了它就会出现「写完库但 chip 条还是旧样子」——用户既看不到
// 刚加的自定义币种，也就点不到那个 × 去删它（一级时 pricingCurrent = -1，函数自己会早退）。
function pricingAfterWrite(message) {
  pricingBuildGroups();
  refreshUsagePricingBadge();
  pricingRenderLevel1();
  pricingRenderCurBar();
  pricingRenderLevel2();
  void loadUsageStats();
  if (message) toast(message);
  return;
}

export function openUsagePricingDialog() {
  const dlg = $('#usagePricingDialog');
  if (!dlg) return;
  pricingDraftIds = new Set();
  pricingCurrent = -1;
  pricingBuildGroups();
  pricingShowLevel1();
  refreshUsagePricingBadge();
  dlg.showModal();
  usageBindDialogViewport(true);
  usageSyncDialogBounds();
}

function pricingShowLevel1() {
  pricingCurrent = -1;
  const level1 = $('#usagePricingLevel1');
  const level2 = $('#usagePricingLevel2');
  if (level1) level1.hidden = false;
  if (level2) level2.hidden = true;
  const back = $('#usagePricingBack');
  if (back) back.hidden = true;
  pricingSetText('usagePricingTitle', '费用单价');
  pricingSetText('usagePricingSub', '按 base_url 分组的 API 供应商 → 模型');
  pricingSetText('usagePricingNote', '保存即生效 · 历史费用按新价重算');
  pricingRenderLevel1();
  const body = pricingDialogBody();
  if (body) body.scrollTop = 0;
}

function pricingShowLevel2(index) {
  const group = pricingGroups[index];
  if (!group) return;
  pricingCurrent = index;
  const level1 = $('#usagePricingLevel1');
  const level2 = $('#usagePricingLevel2');
  if (level1) level1.hidden = true;
  if (level2) level2.hidden = false;
  const back = $('#usagePricingBack');
  if (back) back.hidden = false;
  pricingSetText('usagePricingTitle', pricingGroupTitle(group));
  pricingRenderCurBar();
  pricingRenderLevel2();
  const body = pricingDialogBody();
  if (body) body.scrollTop = 0;
}

function pricingDialogBody() {
  const dlg = $('#usagePricingDialog');
  return dlg ? dlg.querySelector('.pricing-body') : null;
}

// 本段自己的文本写入 helper（usageRenderKpi 里的 setText 是那支函数的局部常量，借不到）。
function pricingSetText(id, text) {
  const el = $(`#${id}`);
  if (el) el.textContent = text;
}

function pricingGroupTitle(group) {
  const lead = group.priced[0] || group.cards[0] || {};
  return String(lead.name || '未命名供应商');
}

function pricingGroupHost(group) {
  return pricingNormUrl(group.url) || '（未填写 API 地址）';
}

/* ---- 一级：分组列表 ---- */
function pricingRenderLevel1() {
  const box = $('#usagePricingGroups');
  if (!box) return;
  if (!pricingGroups.length) {
    box.innerHTML = '<p class="pricing-empty">还没有任何 API 供应商卡片。<br>先到「API 供应商」里添加模型，再回来设单价。</p>';
    return;
  }
  box.innerHTML = pricingGroups.map((group, index) => {
    const total = group.cards.length;
    const priced = group.priced.length;
    const tag = priced === total
      ? `<span class="pricing-tag ok">已定价 ${priced}/${total}</span>`
      : (priced === 0
        ? `<span class="pricing-tag muted">${total} 个未定价</span>`
        : `<span class="pricing-tag warn">${total - priced} 个未定价</span>`);
    const currencies = new Set(group.priced.map((card) => pricingCurrencyOf(card)));
    const curLabel = currencies.size > 1 ? '混合' : ([...currencies][0] || PRICING_DEFAULT_CURRENCY);
    const host = pricingGroupHost(group);
    const lead = pricingGroupTitle(group);
    const initial = Array.from(lead)[0] || '?';
    return `<div class="pricing-group">
      <button class="pricing-group-head" type="button" data-pricing-group="${index}" aria-label="管理 ${escapeHtml(lead)} 的单价">
        <span class="pricing-group-avatar" aria-hidden="true">${escapeHtml(initial)}</span>
        <span class="pricing-group-meta"><b>${escapeHtml(lead)}</b><small title="${escapeHtml(host)}">${escapeHtml(host)} · ${total} 个模型</small></span>
        <span class="pricing-tag muted" title="组内币种">${escapeHtml(curLabel)}</span>
        ${tag}
        <span class="pricing-group-go" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"></path></svg></span>
      </button>
    </div>`;
  }).join('');
}

/* ---- 二级：币种条 + 条目行 + 添加 ---- */
function pricingRenderCurBar() {
  const group = pricingGroups[pricingCurrent];
  const chips = $('#usagePricingCurChips');
  if (!group || !chips) return;
  const used = new Map();
  group.priced.forEach((card) => {
    const cur = pricingCurrencyOf(card);
    used.set(cur, (used.get(cur) || 0) + 1);
  });
  const usedKeys = [...used.keys()];
  const mixed = usedKeys.length > 1;
  const options = [...PRICING_BASE_CURRENCIES, ...usedKeys.filter((cur) => !PRICING_BASE_CURRENCIES.includes(cur))];
  chips.innerHTML = options.map((cur) => {
    const on = !mixed && usedKeys.length === 1 && usedKeys[0] === cur;
    // 非基础档 = 自定义币种：带一个 × 直接移除（把用到它的模型改回默认币种）。
    const removable = !PRICING_BASE_CURRENCIES.includes(cur);
    return `<button class="pricing-cur-chip${on ? ' is-on' : ''}" type="button" data-pricing-currency="${escapeHtml(cur)}" title="整组套用 ${escapeHtml(cur)}">`
      + `${escapeHtml(cur)}${removable ? `<span class="pricing-cur-x" data-pricing-drop-currency="${escapeHtml(cur)}" role="button" title="移除币种 ${escapeHtml(cur)}">✕</span>` : ''}</button>`;
  }).join('') + `<button class="pricing-cur-chip${mixed ? ' is-mixed' : ''}" type="button" data-pricing-custom="1">`
    + (mixed ? `混合 ${escapeHtml(usedKeys.join(' '))}` : '其他…') + '</button>';
  const hint = $('#usagePricingCurHint');
  if (hint) {
    hint.textContent = mixed
      ? '组内币种不一致，可整组统一'
      : (group.priced.length ? '整组套用' : '先添加一个模型定价');
  }
}

function pricingRowMarkup(card, index) {
  const pricing = pricingOf(card);
  const draft = pricingDraftIds.has(card.id);
  const badge = draft
    ? '<span class="pricing-row-badge is-dirty">待填写</span>'
    : '<span class="pricing-row-badge is-saved">已保存</span>';
  const currency = pricingCurrencyOf(card);
  const fields = PRICING_TIER_FIELDS.map((field, tier) => {
    const value = pricing[field];
    const text = (value === undefined || value === null) ? '' : String(value);
    return `<div class="pricing-field">
      <label>${PRICING_TIER_LABELS[tier]}</label>
      <div class="pricing-field-box">
        <input type="number" min="0" max="1000000" step="any" inputmode="decimal" data-pricing-price data-tier="${tier}" value="${escapeHtml(text)}" placeholder="—" aria-label="${PRICING_TIER_LABELS[tier]}单价">
        <button class="pricing-cur-btn" type="button" data-pricing-row-currency title="点一下切换这个模型的币种">${escapeHtml(currency)}</button>
      </div></div>`;
  }).join('');
  return `<div class="pricing-row${draft ? ' is-draft' : ''}" data-pricing-row="${index}" data-pricing-id="${escapeHtml(card.id)}" data-pricing-cur="${escapeHtml(currency)}">
    <div class="pricing-row-head">
      <span class="pricing-row-name" title="${escapeHtml(card.model || '')}">${escapeHtml(card.model || '未指定模型')}</span>
      <span class="pricing-row-card">卡片：${escapeHtml(card.name || '未命名')}</span>
      ${badge}
    </div>
    <div class="pricing-price-grid">${fields}</div>
    <div class="pricing-row-foot">
      <span class="pricing-row-note">按此价计费；改完点保存即刻生效，历史费用一并重算</span>
      <button class="control-button tiny" type="button" data-pricing-remove>删除定价</button>
      <button class="primary-button tiny" type="button" data-pricing-save hidden>保存</button>
    </div>
  </div>`;
}

function pricingRenderLevel2() {
  const group = pricingGroups[pricingCurrent];
  if (!group) return;
  const rows = $('#usagePricingRows');
  if (rows) {
    rows.innerHTML = group.priced.length
      ? group.priced.map((card, index) => pricingRowMarkup(card, index)).join('')
      : '<p class="pricing-empty">这个分组还没有设定单价的模型。<br>点下方「＋ 添加模型定价」开始。</p>';
  }
  const pool = $('#usagePricingPool');
  const poolList = $('#usagePricingPoolList');
  const addBtn = $('#usagePricingAdd');
  if (poolList) {
    poolList.innerHTML = group.pool.map((card, index) => `<button class="pricing-pool-item" type="button" data-pricing-pick="${index}">`
      + `<span class="n">${escapeHtml(card.model || card.id)}</span>`
      + `<span class="p">${escapeHtml(card.name || '未命名')}</span></button>`).join('');
  }
  if (pool) pool.hidden = true;
  if (addBtn) {
    addBtn.hidden = group.pool.length === 0;
    addBtn.textContent = group.pool.length ? `＋ 添加模型定价（${group.pool.length} 个待设）` : '＋ 添加模型定价';
  }
  const note = $('#usagePricingPoolNote');
  if (note) note.textContent = `选择要添加单价的模型（这个地址下尚未定价的 ${group.pool.length} 个卡片）：`;
  pricingSetText('usagePricingSub', `${pricingGroupHost(group)} · ${group.priced.length} 个已定价`
    + (group.pool.length ? ` · ${group.pool.length} 个未定价` : ''));
}

export function pricingTogglePool() {
  const pool = $('#usagePricingPool');
  if (pool) pool.hidden = !pool.hidden;
}

export function pricingOpenCustomCurrency() {
  const box = $('#usagePricingCurCustom');
  if (!box) return;
  box.hidden = false;
  $('#usagePricingCurInput')?.focus();
}

export function pricingCloseCustomCurrency() {
  const box = $('#usagePricingCurCustom');
  const input = $('#usagePricingCurInput');
  if (box) box.hidden = true;
  if (input) input.value = '';
}

// 自定义币种：1–8 个字符，确定后整组套用（与后端 PRICING_CURRENCY_MAX_CHARS 同口径）。
export async function pricingConfirmCustomCurrency() {
  const input = $('#usagePricingCurInput');
  const value = String(input?.value || '').replace(/\s+/g, '').slice(0, 8);
  if (!value) { input?.focus(); return; }
  pricingCloseCustomCurrency();
  await pricingApplyGroupCurrency(value);
}

// 整组套用：把该组所有已定价卡片的币种改成 cur（逐张写库；张数 = 卡片数，通常个位数）。
export async function pricingApplyGroupCurrency(cur) {
  const group = pricingGroups[pricingCurrent];
  if (!group || !group.priced.length) {
    toast('这个分组还没有定价条目，先添加一个模型定价');
    return;
  }
  const targets = group.priced.filter((card) => pricingCurrencyOf(card) !== cur);
  if (!targets.length) return;
  try {
    for (const card of targets) {
      await pricingWrite(card, { ...pricingOf(card), currency: cur });
    }
    pricingAfterWrite(`币种已改为 ${cur}（${targets.length} 个模型）`);
  } catch (error) {
    pricingAfterWrite();
    toast(`改币种失败：${error.message}`);
  }
}

// 移除自定义币种：把用到它的卡片改回默认币种（会改账目口径，所以先确认）。
export async function pricingDropCurrency(cur) {
  const group = pricingGroups[pricingCurrent];
  if (!group) return;
  const targets = group.priced.filter((card) => pricingCurrencyOf(card) === cur);
  if (targets.length && !confirm(`移除币种「${cur}」：${targets.length} 个模型会改回默认币种 ${PRICING_DEFAULT_CURRENCY}，金额口径随之变化。继续吗？`)) {
    return;
  }
  if (!targets.length) { pricingRenderCurBar(); return; }
  try {
    for (const card of targets) {
      await pricingWrite(card, { ...pricingOf(card), currency: PRICING_DEFAULT_CURRENCY });
    }
    pricingAfterWrite(`已移除币种「${cur}」 · ${targets.length} 个模型改回 ${PRICING_DEFAULT_CURRENCY}`);
  } catch (error) {
    pricingAfterWrite();
    toast(`移除币种失败：${error.message}`);
  }
}

export function pricingAddFromPool(index) {
  const group = pricingGroups[pricingCurrent];
  const card = group?.pool?.[index];
  if (!card) return;
  pricingDraftIds.add(card.id);
  pricingBuildGroups();
  pricingRenderLevel1();
  pricingRenderCurBar();
  pricingRenderLevel2();
  const draft = document.querySelector(`.pricing-row[data-pricing-id="${CSS.escape(card.id)}"]`);
  draft?.querySelector('input[data-pricing-price]')?.focus();
}

// 行内改价：标记「未保存」，浮出保存按钮（保存前不写库、不影响统计）。
export function pricingMarkRowDirty(row) {
  if (!row) return;
  row.classList.add('is-dirty');
  const badge = row.querySelector('.pricing-row-badge');
  if (badge) {
    badge.className = 'pricing-row-badge is-dirty';
    badge.textContent = '未保存';
  }
  const save = row.querySelector('[data-pricing-save]');
  if (save) save.hidden = false;
}

// 行内币种：点一下循环切换（¥ → $ → € → 本组自定义 → ¥），只改这一个模型。
export function pricingCycleRowCurrency(row) {
  if (!row) return;
  const group = pricingGroups[pricingCurrent];
  const card = group?.priced?.[Number(row.dataset.pricingRow)];
  if (!card) return;
  const options = [...PRICING_BASE_CURRENCIES];
  const current = row.dataset.pricingCur || PRICING_DEFAULT_CURRENCY;
  if (!options.includes(current)) options.push(current);
  const next = options[(options.indexOf(current) + 1) % options.length];
  row.dataset.pricingCur = next;
  row.querySelectorAll('[data-pricing-row-currency]').forEach((button) => { button.textContent = next; });
  pricingMarkRowDirty(row);
}

// 保存一张卡片：读这一行的三档 + 币种，写库后整层重渲染（保证徽标/金额一致）。
export async function pricingSaveRow(row) {
  if (!row) return;
  const group = pricingGroups[pricingCurrent];
  const card = group?.priced?.[Number(row.dataset.pricingRow)];
  if (!card) return;
  const inputs = [...row.querySelectorAll('[data-pricing-price]')];
  const pricing = { currency: row.dataset.pricingCur || PRICING_DEFAULT_CURRENCY };
  inputs.forEach((input) => {
    const field = PRICING_TIER_FIELDS[Number(input.dataset.tier)];
    const raw = input.value.trim();
    if (raw !== '') pricing[field] = raw;
  });
  if (PRICING_TIER_FIELDS.every((field) => pricing[field] === undefined)) {
    pricingRowNote(row, '三档全空 = 未定价，请至少填一档');
    return;
  }
  const saveBtn = row.querySelector('[data-pricing-save]');
  if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中…'; }
  try {
    await pricingWrite(card, pricing);
    pricingDraftIds.delete(card.id);
    pricingAfterWrite(`已保存 · 「${card.model || card.id}」立即按 ${pricing.currency} 计价`);
  } catch (error) {
    pricingRowNote(row, `保存失败：${error.message}`);
    if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = '保存'; }
  }
}

// 删除定价 = 提交空 pricing（后端删键 ⇒ 未定价，不再计费）。该行回到「添加」池。
export async function pricingRemoveRow(row) {
  if (!row) return;
  const group = pricingGroups[pricingCurrent];
  const card = group?.priced?.[Number(row.dataset.pricingRow)];
  if (!card) return;
  try {
    await pricingWrite(card, {});
    pricingDraftIds.delete(card.id);
    pricingAfterWrite(`已删除定价 · 「${card.model || card.id}」不再计费`);
  } catch (error) {
    pricingRowNote(row, `删除失败：${error.message}`);
  }
}

export function pricingGoBackToList() {
  pricingShowLevel1();
}

export function pricingOpenGroup(index) {
  pricingShowLevel2(index);
}

function pricingRowNote(row, text) {
  const note = row.querySelector('.pricing-row-note');
  if (!note) return;
  note.textContent = text;
  note.classList.add('is-error');
  window.setTimeout(() => {
    note.classList.remove('is-error');
    note.textContent = '按此价计费；改完点保存即刻生效，历史费用一并重算';
  }, 2600);
}

/* ═══════════ 手机端弹层可视视口适配（与 @ 弹层修复同源的问题） ═══════════ */
// 软键盘弹出时 Android Chrome 只缩 visualViewport（布局视口不动），居中 dialog
// 以布局视口定位，底部会被键盘盖住。打开用量弹窗期间监听 vv resize/scroll，把
// dialog 的 max-height 压到可视区内（CSS 的 100dvh 兜底普通小屏，这里兜键盘态）。
function usageSyncDialogBounds() {
  for (const id of ['usageFilterDialog', 'usagePrefsDialog', 'usagePricingDialog']) {
    const dlg = $(`#${id}`);
    if (!dlg || !dlg.open) continue;
    const vv = window.visualViewport;
    if (!vv) {
      dlg.style.removeProperty('max-height');
      continue;
    }
    const avail = Math.max(240, Math.min(vv.height, window.innerHeight - vv.offsetTop) - 16);
    dlg.style.maxHeight = `${avail}px`;
  }
}

function usageDialogViewportListeners() {
  usageSyncDialogBounds();
}

function usageBindDialogViewport(on) {
  const vv = window.visualViewport;
  if (on) {
    window.addEventListener('resize', usageDialogViewportListeners);
    vv?.addEventListener('resize', usageDialogViewportListeners);
    vv?.addEventListener('scroll', usageDialogViewportListeners);
  } else {
    window.removeEventListener('resize', usageDialogViewportListeners);
    vv?.removeEventListener('resize', usageDialogViewportListeners);
    vv?.removeEventListener('scroll', usageDialogViewportListeners);
  }
}

export function openUsageFilterDialog() {
  const dlg = $('#usageFilterDialog');
  if (!dlg) return;
  // 回填当前窗口（所见即所选）：自定义控件反映实况，快选 chip 高亮当前 range。
  const { start, end } = usageWindowBounds();
  const p2 = (n) => String(n).padStart(2, '0');
  const sd = new Date(start);
  const ed = new Date(end);
  const startD = $('#usageFilterStartD');
  const startT = $('#usageFilterStartT');
  const endD = $('#usageFilterEndD');
  const endT = $('#usageFilterEndT');
  if (startD) startD.value = `${sd.getFullYear()}-${p2(sd.getMonth() + 1)}-${p2(sd.getDate())}`;
  if (startT) startT.value = `${p2(sd.getHours())}:${p2(sd.getMinutes())}`;
  if (endD) endD.value = `${ed.getFullYear()}-${p2(ed.getMonth() + 1)}-${p2(ed.getDate())}`;
  if (endT) endT.value = `${p2(ed.getHours())}:${p2(ed.getMinutes())}`;
  const gran = $('#usageFilterGran');
  if (gran) gran.value = usagePrefs.gran;
  const chips = $('#usageFilterChips');
  if (chips) {
    for (const chip of chips.querySelectorAll('[data-usage-filter-range]')) {
      chip.classList.toggle('active', chip.dataset.usageFilterRange === usagePrefs.range);
    }
  }
  dlg.showModal();
  usageBindDialogViewport(true);
  usageSyncDialogBounds();
}

export function applyUsageFilter() {
  const gran = $('#usageFilterGran')?.value;
  if (USAGE_PREF_KEYS.gran.includes(gran)) usagePrefs.gran = gran;
  const startD = $('#usageFilterStartD')?.value;
  const startT = $('#usageFilterStartT')?.value || '00:00';
  const endD = $('#usageFilterEndD')?.value;
  const endT = $('#usageFilterEndT')?.value || '23:59';
  const active = document.querySelector('#usageFilterChips .usage-chip.active');
  const startMs = startD ? new Date(`${startD}T${startT}`).getTime() : 0;
  const endMs = endD ? new Date(`${endD}T${endT}`).getTime() : 0;
  if (startMs && endMs && endMs > startMs) {
    // 填了完整起止时间 = 自定义窗口优先（快选不再生效，弹窗里有说明）。
    usagePrefs.range = 'custom';
    usageCustom = { startMs, endMs: Math.min(endMs, Date.now()) };
  } else if (active && USAGE_PREF_KEYS.range.includes(active.dataset.usageFilterRange)) {
    usagePrefs.range = active.dataset.usageFilterRange;
    usageCustom = null;
  }
  $('#usageFilterDialog')?.close();
  usageUnbindDialogViewport();
  void loadUsageStats();
}

export function resetUsageFilter() {
  usagePrefs.range = USAGE_PREF_DEFAULTS.range;
  usagePrefs.gran = USAGE_PREF_DEFAULTS.gran;
  usageCustom = null;
  $('#usageFilterDialog')?.close();
  usageUnbindDialogViewport();
  void loadUsageStats();
}

export function openUsagePrefsDialog() {
  const dlg = $('#usagePrefsDialog');
  if (!dlg) return;
  const range = $('#usagePrefsRange');
  const gran = $('#usagePrefsGran');
  const chart = $('#usagePrefsChart');
  const metric = $('#usagePrefsMetric');
  if (range) range.value = usagePrefs.range === 'custom' ? USAGE_PREF_DEFAULTS.range : usagePrefs.range;
  if (gran) gran.value = usagePrefs.gran;
  if (chart) chart.value = usagePrefs.chart;
  if (metric) metric.value = usagePrefs.metric;
  dlg.showModal();
  usageBindDialogViewport(true);
  usageSyncDialogBounds();
}

// 偏好保存：与「侧栏分组与排序」同款——POST /api/settings 的 usage_dash 键，
// 服务端白名单校验（config.py），成功后回写 bootstrap 镜像。失败提示不回滚
// （偏好不是数据，重进设置页服务端值会校正回来）。
export async function saveUsagePrefs() {
  const next = {
    range: $('#usagePrefsRange')?.value || usagePrefs.range,
    gran: $('#usagePrefsGran')?.value || usagePrefs.gran,
    chart: $('#usagePrefsChart')?.value || usagePrefs.chart,
    metric: $('#usagePrefsMetric')?.value || usagePrefs.metric,
  };
  const normalized = normalizeUsagePrefs(next);
  const btn = $('#usagePrefsSave');
  const prev = btn?.textContent;
  if (btn) {
    btn.disabled = true;
    btn.textContent = '保存中…';
  }
  try {
    const result = await api('/api/settings', { method: 'POST', body: { usage_dash: normalized } });
    usagePrefs = normalizeUsagePrefs(result?.settings?.usage_dash || normalized);
    if (state.bootstrap?.settings) state.bootstrap.settings.usage_dash = usagePrefs;
    if (usagePrefs.range !== 'custom') usageCustom = null;
    $('#usagePrefsDialog')?.close();
    usageUnbindDialogViewport();
    usagePrefsSynced = true;
    usageRenderAll();
    void loadUsageStats();
    toast('用量默认设置已保存');
  } catch (error) {
    toast(`保存偏好失败：${error.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = prev || '保存偏好设置';
    }
  }
}

export function usageUnbindDialogViewport() {
  usageBindDialogViewport(false);
  const dlg = $('#usageFilterDialog');
  const prefs = $('#usagePrefsDialog');
  dlg?.style.removeProperty('max-height');
  prefs?.style.removeProperty('max-height');
}

// 筛选弹窗内点快选 chip（只改高亮；应用时才生效）。
export function selectUsageFilterRange(range) {
  const chips = $('#usageFilterChips');
  if (!chips) return;
  for (const chip of chips.querySelectorAll('[data-usage-filter-range]')) {
    chip.classList.toggle('active', chip.dataset.usageFilterRange === range);
  }
  // 选了快选：清掉自定义输入，避免「看起来选了 7 天其实还是自定义」的歧义。
  if (USAGE_PREF_KEYS.range.includes(range)) {
    const startD = $('#usageFilterStartD');
    const endD = $('#usageFilterEndD');
    if (startD) startD.value = '';
    if (endD) endD.value = '';
  }
}

/* ---------- 历史数据管理 ---------- */
export async function loadStorageStats() {
  const dbSize = $('#dbSize');
  if (!dbSize) return;
  try {
    const stats = await api('/api/storage/stats');
    dbSize.textContent = formatBytes(Number(stats.db_bytes || 0));
    // 注意 id 是 storageTaskCount：顶栏任务徽标也叫 taskCount（历史重复 id 会让
    // $('#taskCount') 命中顶栏那个，打开设置页时把顶栏文字写成「0 条（已结束 0）」）。
    const tasks = $('#storageTaskCount');
    if (tasks) {
      tasks.textContent = `${Number(stats.task_count || 0)} 条（已结束 ${Number(stats.terminal_task_count || 0)}）`;
    }
    const events = $('#eventCount');
    if (events) {
      events.textContent = `${Number(stats.event_count || 0).toLocaleString()} 条`;
    }
  } catch (error) {
    toast(`统计加载失败：${error.message}`);
  }
}

export async function compactDatabase() {
  const btn = $('#compactDatabase');
  if (!btn) return;
  if (!confirm('压缩数据库会回收已清理记录占用的磁盘空间（VACUUM）。请确认当前没有正在进行的对话任务，期间界面可能短暂卡顿。继续吗？')) return;
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = '压缩中…';
  try {
    const result = await api('/api/storage/compact', { method: 'POST', body: {} });
    toast(`压缩完成：${formatBytes(result.before_bytes)} → ${formatBytes(result.after_bytes)}`);
    await loadStorageStats();
  } catch (error) {
    toast(`压缩失败：${error.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

export function renderWorkspaceControl() {
  const resolved = String(state.bootstrap?.resolved_workspace_dir || '').trim();
  const raw = String(state.bootstrap?.settings?.workspace_dir || 'workspace').trim() || 'workspace';
  const label = raw === 'workspace' ? 'workspace' : (raw.split(/[\\/]/).filter(Boolean).pop() || raw);
  const button = $('#workspaceLabel');
  if (button) button.textContent = label;
  const detail = $('#workspaceDialogResolved');
  if (detail) detail.textContent = resolved || '保存后显示解析路径';
  const input = $('#workspaceDialogInput');
  if (input && document.activeElement !== input) input.value = raw === 'workspace' ? '' : raw;
}

export function workspaceEntryMarkup(entry) {
  const icon = entry.kind === 'directory' ? '▸' : '·';
  return `<button type="button" class="workspace-entry ${entry.kind}" data-workspace-path="${escapeHtml(entry.path)}" data-workspace-kind="${entry.kind}"><span class="workspace-entry-icon">${icon}</span><span class="workspace-entry-name">${escapeHtml(entry.name)}</span>${entry.kind === 'file' && entry.size != null ? `<small>${Number(entry.size).toLocaleString()} B</small>` : ''}</button>`;
}

export async function loadWorkspaceTree(path = '') {
  const tree = $('#workspaceTree');
  if (!tree) return;
  tree.innerHTML = '<p class="activity">正在读取工作区…</p>';
  try {
    const result = await api('/api/workspace/browse?path=' + encodeURIComponent(path || ''));
    state.workspaceBrowsePath = result.path || result.root || '';
    $('#workspaceTreePath').textContent = state.workspaceBrowsePath || '-';
    $('#workspaceTreeTitle').textContent = (state.workspaceBrowsePath.split(/[\\/]/).filter(Boolean).pop() || '当前工作区');
    $('#workspaceUp').disabled = !result.parent;
    tree.innerHTML = result.entries?.length ? result.entries.map(workspaceEntryMarkup).join('') : '<p class="activity">此目录为空</p>';
    $$('#workspaceTree .workspace-entry').forEach((button) => button.addEventListener('dblclick', () => {
      if (button.dataset.workspaceKind === 'directory') loadWorkspaceTree(button.dataset.workspacePath);
    }));
    $$('#workspaceTree .workspace-entry').forEach((button) => button.addEventListener('click', () => {
      if (button.dataset.workspaceKind === 'directory') loadWorkspaceTree(button.dataset.workspacePath);
      else { $('#workspaceDialogInput').value = result.root || ''; toast(`已选中文件：${button.querySelector('.workspace-entry-name')?.textContent || ''}`); }
    }));
  } catch (error) { tree.innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`; }
}

export async function loadMcpServers() {
  const data = await api('/api/mcp');
  state.bootstrap.mcp_servers = data.servers || [];
  renderMcp();
  return state.bootstrap.mcp_servers;
}

export function populateVisionSettings() {
  const settings = state.bootstrap.settings || {};
  const vision = settings.vision || {};
  const select = $('#visionProvider');
  if (select) {
    const providers = state.bootstrap.model_profiles || state.bootstrap.providers || [];
    const previous = select.value;
    select.replaceChildren(new Option('OVH 免费视觉链（默认）', ''));
    for (const kind of ['online', 'local']) {
      const list = providers.filter((p) => (p.kind || 'online') === kind);
      if (!list.length) continue;
      const group = document.createElement('optgroup');
      group.label = kind === 'local' ? '本地 API / 模型' : '在线 API';
      for (const provider of list) {
        const option = new Option(`${provider.name || provider.id} · ${provider.model || ''}`, provider.model_key || provider.id);
        group.append(option);
      }
      select.append(group);
    }
    const target = vision.provider_model_key || previous || '';
    if ([...select.options].some((option) => option.value === target)) select.value = target;
    const deleteButton = $('#deleteVisionProvider');
    if (deleteButton) deleteButton.disabled = !select.value;
  }
  const timeout = $('#visionTimeout'); if (timeout) timeout.value = vision.timeout_ms || 180000;
  const maxImages = $('#visionMaxImages'); if (maxImages) maxImages.value = vision.max_images || 4;
}

export function populateSearchSettings() {
  const settings = state.bootstrap.settings || {};
  const search = settings.search || {};
  const profiles = searchProfiles(search);
  const select = $('#searchProfileSelect');
  if (!select) return;
  select.replaceChildren(...profiles.map((profile) => new Option(profile.name || profile.endpoint || '未命名搜索 API', profile.id)));
  if (!profiles.length) select.append(new Option('尚未添加搜索 API', ''));
  const target = profiles.some((profile) => profile.id === search.provider_id)
    ? search.provider_id
    : (profiles[0]?.id || '');
  select.value = target;
  renderSearchProfileFields(profiles.find((profile) => profile.id === target) || {});
  $('#deleteSearchProfile').disabled = !target;
  // web_search 可用性诊断：是否已配置端点 + 是否为当前会话工具集可用（只读提示）
  const active = profiles.find((profile) => profile.id === target) || profiles[0] || null;
  const endpointConfigured = Boolean(active?.endpoint?.trim());
  const status = $('#searchAvailability');
  if (status) {
    status.textContent = endpointConfigured
      ? `web_search 可用性：端点已配置 ✓（${(active.endpoint || '').slice(0, 48)}）。只要 Agent 工具集包含 web_search，模型即可调用。`
      : `web_search 可用性：端点未配置 ✗ —— web_search 工具不会出现在工具清单中。请填写“端点 URL”并点击“测试搜索连接”。`;
  }
}

export function searchProfiles(search = state.bootstrap?.settings?.search || {}) {
  if (Array.isArray(search.profiles) && search.profiles.length) {
    return search.profiles.map((profile) => ({ ...profile }));
  }
  if (search.endpoint) {
    return [{
      id: 'legacy-search',
      name: '搜索 API',
      endpoint: search.endpoint,
      api_key: search.api_key || '',
      max_results: search.max_results || 5,
    }];
  }
  return [];
}

export function renderSearchProfileFields(profile) {
  $('#searchProfileName').value = profile.name || '';
  $('#searchEndpoint').value = profile.endpoint || '';
  $('#searchApiKey').value = profile.api_key || '';
  $('#searchMaxResults').value = profile.max_results || 5;
}

export function searchProfileFormValue(id = '') {
  return {
    id: id || `search_${Date.now().toString(36)}`,
    name: $('#searchProfileName')?.value.trim() || '搜索 API',
    endpoint: $('#searchEndpoint')?.value.trim() || '',
    api_key: $('#searchApiKey')?.value.trim() || '',
    max_results: Number($('#searchMaxResults')?.value || 5),
  };
}

export async function saveVisionSettings(options = {}) {
  const payload = {
    vision: {
      provider_model_key: $('#visionProvider')?.value || '',
      timeout_ms: Number($('#visionTimeout')?.value || 180000),
      max_images: Number($('#visionMaxImages')?.value || 4),
    },
  };
  try {
    const result = await api('/api/settings', { method: 'POST', body: payload });
    Object.assign(state.bootstrap.settings, result.settings);
    const deleteButton = $('#deleteVisionProvider');
    if (deleteButton) deleteButton.disabled = !payload.vision.provider_model_key;
    if (!options.quiet) toast('视觉设置已保存');
  } catch (error) {
    toast(`视觉设置保存失败：${error.message}`);
  }
}

export function selectedVisionProvider() {
  const key = $('#visionProvider')?.value || '';
  return (state.bootstrap.model_profiles || state.bootstrap.providers || [])
    .find((provider) => (provider.model_key || provider.id) === key) || null;
}

export function openVisionProviderForm() {
  switchSettingsTab('models');
  addProvider();
}

export async function deleteVisionProvider() {
  const provider = selectedVisionProvider();
  if (!provider) return;
  if (!confirm(`删除 API 供应商“${provider.name || provider.id}”？这会同时移除模型配置。`)) return;
  try {
    await api(`/api/providers/${encodeURIComponent(provider.id)}`, { method: 'DELETE' });
    const data = await api('/api/bootstrap');
    state.bootstrap = { ...state.bootstrap, ...data };
    populateModels();
    renderProviders();
    populateVisionSettings();
    toast('API 供应商已删除');
  } catch (error) {
    toast(`删除 API 失败：${error.message}`);
  }
}

export async function persistSearchProfiles(profiles, providerId, quiet = false) {
  const payload = { search: { provider_id: providerId || '', profiles } };
  const result = await api('/api/settings', { method: 'POST', body: payload });
  Object.assign(state.bootstrap.settings, result.settings);
  populateSearchSettings();
  if (!quiet) toast('搜索 API 已保存');
}

export async function saveSearchSettings(options = {}) {
  const search = state.bootstrap.settings.search || {};
  const profiles = searchProfiles(search);
  let id = $('#searchProfileSelect')?.value || '';
  const profile = searchProfileFormValue(id);
  id = profile.id;
  const index = profiles.findIndex((item) => item.id === id);
  if (index >= 0) profiles[index] = profile;
  else profiles.push(profile);
  await persistSearchProfiles(profiles, id, options.quiet === true);
}

export function addSearchProfile() {
  const search = state.bootstrap.settings.search || {};
  const profiles = searchProfiles(search);
  const profile = { id: `search_${Date.now().toString(36)}`, name: '新搜索 API', endpoint: '', api_key: '', max_results: 5 };
  profiles.push(profile);
  state.bootstrap.settings.search = { provider_id: profile.id, profiles };
  populateSearchSettings();
  $('#searchProfileName').select();
}

export async function deleteSearchProfile() {
  const id = $('#searchProfileSelect')?.value || '';
  if (!id) return;
  const search = state.bootstrap.settings.search || {};
  const profiles = searchProfiles(search);
  const current = profiles.find((profile) => profile.id === id);
  if (!confirm(`删除搜索 API“${current?.name || ''}”？`)) return;
  const remaining = profiles.filter((profile) => profile.id !== id);
  await persistSearchProfiles(remaining, remaining[0]?.id || '');
}

export async function testVisionCapability(probe) {
  const el = $('#visionTestResult');
  if (el) el.textContent = '测试中…';
  try {
    const result = await api('/api/vision/test', {
      method: 'POST',
      body: { provider_model_key: $('#visionProvider')?.value || '', probe },
    });
    if (el) {
      const label = '视觉识别';
      const errors = {
        connection: '服务连接失败',
        text_inference: '文本推理失败',
        image_load: '图片加载失败',
        vision_capability: '视觉能力不可用',
        unknown: '未知错误',
      };
      el.textContent = result.ok
        ? `${label}可用（延迟 ${result.latency_ms ?? '?'}ms，${result.backend || result.model || ''}）`
        : `${label}不可用 [${errors[result.error_kind] || errors.unknown}]：${result.reason || ''}${result.hint ? `；${result.hint}` : ''}`;
    }
  } catch (error) {
    if (el) el.textContent = `测试失败：${error.message}`;
  }
}

export async function testVisionConnection() {
  return testVisionCapability('vision');
}

export async function testSearchConnection() {
  const el = $('#searchTestResult');
  if (el) el.textContent = '测试中…';
  try {
    const result = await api('/api/search/test', {
      method: 'POST',
      body: searchProfileFormValue($('#searchProfileSelect')?.value || ''),
    });
    if (el) el.textContent = result.ok ? `可用（provider=${result.provider || ''}）` : `不可用：${result.reason || ''}`;
  } catch (error) {
    if (el) el.textContent = `测试失败：${error.message}`;
  }
}

// ---- Agent 管理 ----

export async function refreshAgentsFromServer() {
  const data = await api('/api/agents');
  state.bootstrap.agents = data.agents || [];
  state.bootstrap.default_agent_id = data.default_agent_id || 'master';
}

// 卡片上的提示词摘要长度（超出截断，完整内容在弹层里看）。
const AGENT_PROMPT_PREVIEW_LIMIT = 140;

// 弹层里新选的头像文件（保存时才上传；新建 Agent 此时还没有 id，必须延后到保存后）。
let agentAvatarFile = null;

// 上传头像的 URL（内置 Agent 的 emoji 头像走 agentAvatarEmoji，不在这里）。
export function agentAvatarUrl(agent) {
  return agentAvatarSrc(agent);
}

function agentCardMarkup(agent) {
  const id = escapeHtml(agent.id || '');
  const name = escapeHtml(agent.name || '未命名 Agent');
  const skills = Array.isArray(agent.skill_ids) ? agent.skill_ids.length : 0;
  const prompt = String(agent.system_prompt || '').trim();
  const preview = prompt.length > AGENT_PROMPT_PREVIEW_LIMIT
    ? `${prompt.slice(0, AGENT_PROMPT_PREVIEW_LIMIT)}…`
    : prompt;
  const avatar = agentAvatarUrl(agent);
  // 内置 Agent 用 emoji 当头像（avatar 字段存字形）；上传过图片的走 <img>。两者互斥。
  const avatarEmoji = agentAvatarEmoji(agent);
  const avatarHtml = avatar
    ? `<img class="agent-card-avatar" src="${escapeHtml(avatar)}" alt="">`
    : (avatarEmoji ? `<span class="agent-card-avatar agent-card-avatar-emoji">${escapeHtml(avatarEmoji)}</span>` : '');
  // 卡片上不再标「默认」角标、也不给默认 Agent 加高亮：默认项由顶栏 Agent 选择器体现，
  // 卡片上一旦有强调色/角标，会被误读成"当前选中/正在编辑的那一个"。
  const badges = [
    agent.built_in ? '<span class="agent-card-tag">内置</span>' : '',
  ].join('');
  const toolSetName = toolScopeLabel(agent.tool_scope);
  return `
    <div class="agent-card" data-agent-card="${id}" role="button" tabindex="0" aria-label="编辑 ${name}">
      ${agent.built_in ? '' : `<button class="agent-card-delete" type="button" data-agent-delete="${id}" title="删除 ${name}" aria-label="删除 ${name}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>`}
      <span class="agent-card-name" title="${name}">${avatarHtml}${name}</span>
      <span class="agent-card-meta">${skills ? `${skills} 个固定 Skill` : '无固定 Skill'}</span>
      <span class="agent-card-tools" title="该 Agent 的工具集：${escapeHtml(toolSetName)}">工具集：${escapeHtml(toolSetName)}</span>
      <p class="agent-card-prompt">${preview ? escapeHtml(preview) : '未设置系统提示词'}</p>
      <span class="agent-card-foot">${badges}</span>
    </div>`;
}

export async function renderAgentManager() {
  const list = $('#agentCards');
  if (!list) return;
  // 卡片要显示「工具集：预设名」，先确保工具目录已加载（只拉一次，失败下次再试）。
  await ensureToolCatalog();
  const agents = state.bootstrap?.agents || [];
  // 「新增 Agent」卡片固定排在最后一张（列表为空时它就是唯一一张卡）。
  list.innerHTML = agents.map((agent) => agentCardMarkup(agent)).join('') + `
    <button type="button" class="agent-card agent-card-add" data-agent-add>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg>
      <span>新增 Agent</span>
    </button>`;
}

export function openAgentCard(agentId) {
  const agent = (state.bootstrap?.agents || []).find((item) => item.id === agentId);
  if (!agent) return;
  showAgentForm(agent);
}

// Agent 弹层分区切换（基本 / 系统提示词 / 固定 Skill / 工具集）。
// 四块面板都留在 DOM 里、只切 hidden —— 切页不丢勾选与已输入内容；快捷提示词面板随切页收起。
export function switchAgentTab(name) {
  $$('.agent-tabs button[data-agent-tab]').forEach((button) => {
    const active = button.dataset.agentTab === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  $$('.agent-tab-panel').forEach((panel) => { panel.hidden = panel.dataset.agentPanel !== name; });
  closeAgentPromptPresetPanel();
}

export function updateAgentSkillTabCount() {
  const count = $('#agentSkillTabCount');
  if (count) count.textContent = String(state.agentFormSkillIds.length);
}

export function renderAgentSkillPicker() {
  const list = $('#agentSkillList');
  if (!list) return;
  const skills = state.bootstrap?.skills || [];
  // 与工具集同款卡片：勾选框 + 名称 + 两行说明（.skill-card，不复用技能页的 .skill-item 列表样式）。
  list.innerHTML = skills.map((skill) => `
    <label class="skill-card">
      <input type="checkbox" value="${skill.id}" ${state.agentFormSkillIds.includes(skill.id) ? 'checked' : ''}>
      <span><b>${escapeHtml(skill.name)}</b><p>${escapeHtml(skill.description)}</p></span>
    </label>`).join('') || '<p class="activity">暂无可用 Skill</p>';
  updateAgentSkillTabCount();
}

export function showAgentForm(agent = null) {
  $('#agentFormId').value = agent?.id || '';
  $('#agentName').value = agent?.name || '';
  $('#agentSystemPromptEdit').value = agent?.system_prompt || '';
  state.agentFormSkillIds = agent?.skill_ids ? [...agent.skill_ids] : [];
  state.agentFormIsNew = !agent;
  state.agentFormToolScope = agent?.tool_scope ? [...agent.tool_scope] : [];
  state.agentFormUnknownTools = [];
  // 旧 Agent 的 tool_scope 为空 = 不限制（运行时全放行，且以后新增的工具自动纳入）。
  // 界面上按“全选”展示，但只要用户没动过勾选就仍以空数组保存，避免被固化成死列表。
  state.agentFormUnrestricted = Boolean(agent) && !state.agentFormToolScope.length;
  state.agentFormScopeTouched = false;
  // 搜索框每次打开表单复位（否则会残留上一次的关键词，只看到过滤后的工具）。
  state.agentToolFilter = '';
  const toolFilter = $('#agentToolFilter');
  if (toolFilter) toolFilter.value = '';
  // 表单每次打开由 renderAgentToolPicker 重建预设下拉框与模板行；
  // 「存为模板」控件随下方勾选实时显隐（自定义组合时出现）。
  renderAgentSkillPicker();
  renderAgentToolPicker();
  // 快捷提示词面板每次打开表单收起并重建列表（套用结果只进文本框，保存 Agent 才落库）。
  closeAgentPromptPresetPanel();
  renderAgentPromptPresetList();
  // 分区复位到「基本」：避免上一次停留的页残留观感。
  switchAgentTab('basic');
  $('#agentError').textContent = '';
  // 卡片点开即编辑；ID 由后台分配，只在副标题里显示已有 ID 供核对。
  agentAvatarFile = null;
  const existingAvatar = agentAvatarUrl(agent);
  const avatarPreview = $('#agentAvatarPreview');
  if (avatarPreview) {
    avatarPreview.hidden = !existingAvatar;
    avatarPreview.src = existingAvatar;
    avatarPreview.title = existingAvatar ? '当前头像' : '';
  }
  // emoji 头像（内置 Agent）用一个文本节点预览：<img> 画不出字形。
  const existingEmoji = agentAvatarEmoji(agent);
  const emojiPreview = $('#agentAvatarEmojiPreview');
  if (emojiPreview) {
    emojiPreview.hidden = !existingEmoji;
    emojiPreview.textContent = existingEmoji;
  }
  $('#agentDialogTitle').textContent = state.agentFormIsNew ? '新增 Agent' : (agent?.name || 'Agent 设置');
  $('#agentDialogSubtitle').textContent = state.agentFormIsNew
    ? '保存后自动分配 ID'
    : `Agent ID：${agent?.id || ''}`;
  const dialog = $('#agentDialog');
  if (dialog && !dialog.open) dialog.showModal();
  // 弹层确实打开后才聚焦：往关闭的弹层里 focus() 会让浏览器把文档滚到弹层所在位置（幽灵面板/整页
  // 偏移都由它触发），也避免将来弹层取消失败时把页面滚走。
  if (!dialog || dialog.open) $('#agentName').focus();
}

// 「自定义头像」：选图后只做本地预览，保存 Agent 时才真正上传（新建时还没有 id）。
export function pickAgentAvatar() {
  $('#agentAvatarFileInput')?.click();
}

export function handleAgentAvatarFile(file) {
  if (!file) return;
  if (!String(file.type || '').startsWith('image/')) {
    toast('请选择图片文件（PNG / JPG / WebP）');
    return;
  }
  agentAvatarFile = file;
  const preview = $('#agentAvatarPreview');
  if (preview) {
    preview.hidden = false;
    preview.src = URL.createObjectURL(file);
    preview.title = `待保存：${file.name || '头像'}`;
  }
  // 选了新图就顶掉 emoji 头像：保存后 avatar 字段会变成文件名，字形预览必须同步收起。
  const emojiPreview = $('#agentAvatarEmojiPreview');
  if (emojiPreview) {
    emojiPreview.hidden = true;
    emojiPreview.textContent = '';
  }
  toast('头像已选择，点「保存 Agent」后生效');
}

// Agent 设置：工具选择的联动规则。创建者工具依赖其查询工具（与后端
// JOB_CREATOR_TOOL_DEPS / 依赖闭包保持一致）。选中创建者时自动带上查询工具；
// 取消某个查询工具时，若仍有选中的创建者依赖它，则同步取消该创建者，保证
// “创建者被允许 ⇔ 其描述里让你查询的工具也被允许”的 invariant 不被打破。
export const AGENT_TOOL_DEP_RULES = {
  run_in_background: ['job_output', 'job_status', 'job_wait', 'job_kill'],
  subagent: ['job_output'],
  subagent_spawn: ['job_output'],
  comfyui_batch: ['job_output', 'job_status', 'job_wait'],
};

// 互斥组：同一组里只能勾选一个工具（与后端 naiba/run/session.py 的
// MUTUALLY_EXCLUSIVE_TOOL_GROUPS 逐字镜像 —— 改一边必须改另一边）。
// 子代理的两种上下文模式就是这一组：subagent 继承会话历史（fork）/ subagent_spawn
// 干净上下文（spawn）。组内**排前者**是保留方向（fork 选错只是费钱，反过来丢上下文
// 是质量事故），批量勾选（分类全选）冲突时按它归一。
export const AGENT_TOOL_MUTEX_GROUPS = [['subagent', 'subagent_spawn']];

// 工具 → 同组其它工具（勾一个就取消另一个）。
const MUTEX_RIVALS = new Map();
for (const group of AGENT_TOOL_MUTEX_GROUPS) {
  for (const name of group) MUTEX_RIVALS.set(name, group.filter((n) => n !== name));
}

// 互斥归一：两个都在时只留组内排前者（与后端 normalize_tool_mutex 同口径）。
export function normalizeToolMutex(scope) {
  const result = new Set(scope || []);
  for (const group of AGENT_TOOL_MUTEX_GROUPS) {
    const present = group.filter((name) => result.has(name));
    present.slice(1).forEach((name) => result.delete(name));
  }
  return [...result];
}

export function applyAgentToolDependency(scope, changedTool, checked, { applyMutex = true } = {}) {
  const result = new Set(scope);
  if (checked) {
    result.add(changedTool);
    const deps = AGENT_TOOL_DEP_RULES[changedTool];
    if (deps) deps.forEach((dep) => result.add(dep));
    // 互斥：单点勾选时以用户刚点的这个为准（批量全选走 normalizeToolMutex，
    // 那里按组内排前者保留 fork —— 全选没有"用户想选哪个"的语义）。
    if (applyMutex) (MUTEX_RIVALS.get(changedTool) || []).forEach((rival) => result.delete(rival));
  } else {
    result.delete(changedTool);
    // 取消的若是某创建者必需的查询工具，则把这些创建者也一并取消。
    for (const [creator, deps] of Object.entries(AGENT_TOOL_DEP_RULES)) {
      if (deps.includes(changedTool) && result.has(creator)) result.delete(creator);
    }
  }
  return [...result];
}

// 把工具集补齐依赖闭包 + 互斥归一：选中创建者工具时自动带上它依赖的查询工具；
// 互斥组冲突时只留排前者。与后端依赖闭包/互斥归一保持一致，保证这里勾选的状态
// 就是运行时会放行的 allowed_tools。
export function normalizeToolScope(scope) {
  const result = new Set(scope || []);
  for (const [creator, deps] of Object.entries(AGENT_TOOL_DEP_RULES)) {
    if (result.has(creator)) deps.forEach((dep) => result.add(dep));
  }
  return normalizeToolMutex([...result]);
}

// 批量勾选（分类/二级分组全选）后的互斥提示：整组全选会同时命中互斥的两个子代理工具，
// 这里按「保留组内排前者」归一，并提示一次 —— 否则用户看不出为什么少勾了一个。
function notifyToolMutexDrop(before, after) {
  const dropped = (before || []).filter((name) => !after.includes(name));
  if (!dropped.length) return;
  toast('子 Agent 有两种上下文模式，互斥只能开一个：已保留 subagent（继承会话历史）');
}

// —— 工具集：卡片态（内置预设 + 我的工具集）↔ 编辑态 ——
// 两态同框叠放、弹层高度固定：点任意卡片（含「添加」卡）都先按一下再向上滑出，工具列表从下方滑入。
const TOOL_SWAP_MS = 130;  // = CSS 里 .tool-preset-view.is-leaving 的 50ms 延迟 + 80ms 过渡
const TOOL_SET_ADD_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg>';
const TOOL_SET_DEL_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>';

// 一套工具（Agent 的 tool_scope 或工具集的 tools）参与匹配前的归一：滤掉「当前未注册」的幽灵名
// （退役工具、掉线的 MCP 工具）。matchToolScope / toolScopeLabel / 删除工具集前的引用判定
// 都走它，匹配口径只有这一处：幽灵名算进去只会让同一套工具因为几个死名字匹配不上。
function matchableTools(list) {
  const raw = Array.isArray(list) ? list.filter(Boolean) : [];
  const known = knownToolNames();
  return known.size ? raw.filter((name) => known.has(name)) : raw;
}

// 当前工具集命中哪张卡片：内置预设优先，其次「我的工具集」；都不匹配返回 null。
export function matchToolScope(scope) {
  const current = new Set(matchableTools(scope));
  const same = (tools) => tools.length === current.size && tools.every((t) => current.has(t));
  const preset = (state.toolCatalog?.presets || []).find((item) => same(item.tools || []));
  if (preset) return { kind: 'preset', id: preset.id, name: preset.name, tools: preset.tools || [] };
  const template = loadToolTemplates().find((item) => same(usableTemplateTools(item)));
  if (template) return { kind: 'template', id: template.id, name: template.name, tools: template.tools || [] };
  return null;
}

export function matchToolPreset() {
  return matchToolScope(state.agentFormToolScope);
}

// 某个工具集（Agent 的 tool_scope）对应的名称：预设名 → 我的工具集名 → 未限制 / 自定义。
// Agent 卡片用它在「固定 Skill」下方显示这一栏，一眼看出这个 Agent 开的是哪套工具。
export function toolScopeLabel(scope) {
  const raw = Array.isArray(scope) ? scope.filter(Boolean) : [];
  if (!raw.length) return '未限制（全部工具）';
  const tools = matchableTools(raw);
  if (!tools.length) return `自定义 · ${raw.length} 个工具`;
  const matched = matchToolScope(tools);
  if (matched) return matched.name;
  return `自定义 · ${tools.length} 个工具`;
}

function toolSetSummary() {
  if (state.agentFormUnrestricted && !state.agentFormScopeTouched) {
    return '当前：未限制（等同全能，以后新增的工具自动包含）';
  }
  const total = state.agentFormToolScope.length;
  const matched = matchToolPreset();
  if (matched) return `当前：${matched.name} · ${total} 个工具`;
  return `当前：自定义（未保存）· ${total} 个工具`;
}

// 卡片态渲染：4 张内置预设（只读）+ 我的工具集（可删）+ 末尾「添加」卡 + 一行摘要。
export function renderAgentToolPresetCards() {
  const box = $('#agentToolPresetCards');
  if (!box) return;
  const matched = matchToolPreset();
  const presetCards = (state.toolCatalog?.presets || []).map((preset) => {
    const active = matched?.kind === 'preset' && matched.id === preset.id;
    return `<div class="tool-preset-card${active ? ' is-active' : ''}" role="button" tabindex="0"
      data-tool-preset-card="${escapeHtml(preset.id)}" title="套用「${escapeHtml(preset.name)}」">
      <b>${escapeHtml(preset.name)}</b>
      <small>${escapeHtml(preset.desc || '')}</small>
      <em>${(preset.tools || []).length} 个工具</em>
    </div>`;
  });
  const templateCards = loadToolTemplates().map((template) => {
    const active = matched?.kind === 'template' && matched.id === template.id;
    const usable = usableTemplateTools(template);
    const count = usable.length;
    const name = escapeHtml(template.name);
    // 第二行给工具名预览：只写「我的工具集」等于零信息，用户看不出这套里装了什么。
    const preview = usable.slice(0, 3).join('、') + (usable.length > 3 ? ' …' : '');
    return `<div class="tool-preset-card${active ? ' is-active' : ''}" role="button" tabindex="0"
      data-tool-template-card="${escapeHtml(template.id)}"
      title="套用「${name}」（${count} 个工具）${usable.length ? `：${escapeHtml(usable.join('、'))}` : ''}">
      <button type="button" class="tool-preset-card-del" data-tool-template-del="${escapeHtml(template.id)}"
              title="删除工具集「${name}」" aria-label="删除工具集「${name}」">${TOOL_SET_DEL_SVG}</button>
      <b>${name}</b>
      <small>${escapeHtml(preview || '（工具已全部失效）')}</small>
      <em>我的工具集 · ${count} 个工具</em>
    </div>`;
  });
  box.innerHTML = [...presetCards, ...templateCards].join('') + `
    <div class="tool-preset-card tool-preset-card-add" role="button" tabindex="0"
         data-tool-preset-add title="以当前勾选的 ${state.agentFormToolScope.length} 个工具为底稿，另存一套自定义工具集">
      ${TOOL_SET_ADD_SVG}
      <span>添加自定义工具集</span>
    </div>`;
  const summary = $('#agentToolPresetState');
  if (summary) summary.textContent = toolSetSummary();
  renderAgentToolPeek();
  updateToolCounter();
}

// 当前已选工具按分类摊平：[{ name: 分类名, tools: [工具名…] }]。
// 卡片态把它直接列在摘要下面 —— 已配好的 Agent 再次打开时，先看清「用了什么」再谈要不要改。
// 依赖工具目录的 groups（`group.tools` 含 MCP 二级分组的成员），目录里没归类的兜底进「其他」。
export function toolScopeBreakdown() {
  const scope = state.agentFormToolScope || [];
  if (!scope.length) return [];
  const selected = new Set(scope);
  const placed = new Set();
  const rows = [];
  for (const group of state.toolCatalog?.groups || []) {
    const tools = (group.tools || []).filter((name) => selected.has(name));
    if (!tools.length) continue;
    tools.forEach((name) => placed.add(name));
    rows.push({ name: group.name, tools });
  }
  const rest = scope.filter((name) => !placed.has(name));
  if (rest.length) rows.push({ name: '其他', tools: rest });
  return rows;
}

// 只读清单：分组名 + 该组用到的工具名（顿号连接）。工具名多时靠容器滚动，不撑破弹层。
export function renderAgentToolPeek() {
  const box = $('#agentToolPeekList');
  if (!box) return;
  const rows = toolScopeBreakdown();
  if (!rows.length) {
    box.innerHTML = '<p class="tool-peek-empty">当前没有勾选任何工具（未限制 = 全部工具）。</p>';
    return;
  }
  const unknown = state.agentFormUnknownTools || [];
  box.innerHTML = rows.map((row) => `<div class="tool-peek-group">
      <b>${escapeHtml(row.name)}</b>
      <span>${escapeHtml(row.tools.join('、'))}</span>
    </div>`).join('') + (unknown.length
    ? `<div class="tool-peek-group is-unknown"><b>已失效</b><span>${escapeHtml(unknown.join('、'))}</span></div>`
    : '');
}

// 打开表单时清单回到展开态（「收起」是临时动作，不跨表单残留）。
export function resetAgentToolPeek() {
  const box = $('#agentToolPeekList');
  if (box) box.hidden = false;
  const btn = $('#toggleAgentToolPeek');
  if (btn) {
    btn.textContent = '收起清单';
    btn.setAttribute('aria-expanded', 'true');
  }
}

export function toggleAgentToolPeek() {
  const box = $('#agentToolPeekList');
  if (!box) return;
  const show = box.hidden;
  box.hidden = !show;
  const btn = $('#toggleAgentToolPeek');
  if (btn) {
    btn.textContent = show ? '收起清单' : '展开清单';
    btn.setAttribute('aria-expanded', show ? 'true' : 'false');
  }
}

// 勾选变化时刷新：卡片态重绘卡片（高亮/计数）；编辑态只更新摘要（面板隐藏，别白重绘）。
export function updateToolPresetUI() {
  const editor = $('#agentToolEditor');
  if (editor && !editor.hidden) {
    const summary = $('#agentToolPresetState');
    if (summary) summary.textContent = toolSetSummary();
    updateToolCounter();
    return;
  }
  renderAgentToolPresetCards();
}

export function updateToolCounter() {
  const total = (state.toolCatalog?.tools || []).length;
  const counter = $('#agentToolCount');
  const unrestricted = state.agentFormUnrestricted && !state.agentFormScopeTouched;
  if (counter) {
    counter.textContent = unrestricted
      ? `全部 ${total} 个（未限制）`
      : `已选 ${state.agentFormToolScope.length} / ${total} 个工具`;
  }
  // 分区标签上的计数：切到别的页也能一眼看到勾了多少。
  const tabCount = $('#agentToolTabCount');
  if (tabCount) {
    tabCount.textContent = unrestricted
      ? `${total}/${total}`
      : `${state.agentFormToolScope.length}/${total}`;
  }
  updateToolOnlySelectedButton(unrestricted ? total : state.agentFormToolScope.length);
}

// 「只看已选 N」按钮：数量随勾选实时变（它是编辑态里核对"这一套到底开了什么"的入口）。
export function updateToolOnlySelectedButton(count) {
  const btn = $('#agentToolOnlySelected');
  if (!btn) return;
  const total = typeof count === 'number' ? count : (state.agentFormToolScope || []).length;
  btn.textContent = `只看已选 ${total}`;
  btn.setAttribute('aria-pressed', state.agentToolOnlySelected ? 'true' : 'false');
  btn.classList.toggle('is-active', Boolean(state.agentToolOnlySelected));
}

export function toggleToolOnlySelected() {
  state.agentToolOnlySelected = !state.agentToolOnlySelected;
  // 两份清单互斥：只看已选时搜索框不参与（否则两份过滤叠加，结果没人看得懂）。
  if (state.agentToolOnlySelected) {
    state.agentToolFilter = '';
    const filter = $('#agentToolFilter');
    if (filter) filter.value = '';
  }
  renderToolScopeList();
}

// 卡片态 ↔ 编辑态切换：卡片向上滑出、编辑区从下方滑入（返回时反向）。
// 两态叠在同一个 .tool-stage 格子里，弹层高度不参与变化，所以没有布局跳动。
function swapToolView(editing) {
  const view = $('#agentToolPresetView');
  const editor = $('#agentToolEditor');
  if (!view || !editor) return;
  if (editing === !editor.hidden) return; // 已在目标态
  const reduce = Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches);
  const outgoing = editing ? view : editor;
  const incoming = editing ? editor : view;
  const finish = () => {
    outgoing.hidden = true;
    outgoing.classList.remove('is-leaving', 'is-entering');
    incoming.hidden = false;
    incoming.classList.remove('is-leaving', 'is-entering');
  };
  if (reduce) {
    finish();
    return;
  }
  outgoing.classList.add('is-leaving');
  window.setTimeout(() => {
    finish();
    incoming.classList.add('is-entering');
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      incoming.classList.remove('is-entering');
    }));
  }, TOOL_SWAP_MS);
}

// 进入编辑态：
//   内置预设卡 → 载入该预设的工具（走依赖闭包）、命名栏预填预设名、保存时另存为「我的工具集」；
//   我的工具集卡 → 载入该套工具、命名栏预填它的名字、保存时原地更新；
//   「添加」卡 → 以**当前勾选**为底稿（未限制时 = 当前目录全量）、命名栏留空（留空自动命名）。
//   若只想带着当前勾选进编辑器而不另存，走 openAgentToolEditorCurrent()（不碰勾选）。
export function openAgentToolEditor({ presetId = '', templateId = '' } = {}) {
  const presets = state.toolCatalog?.presets || [];
  const preset = presetId ? presets.find((item) => item.id === presetId) : null;
  const template = templateId
    ? loadToolTemplates().find((item) => item.id === templateId) : null;
  const base = preset || null;
  state.agentToolEditingId = template ? template.id : '';
  if (base) {
    setAgentToolScope(normalizeToolScope(base.tools || []));
  } else if (template) {
    const tools = usableTemplateTools(template);
    if (!tools.length) {
      toast('该工具集里的工具当前都已不存在，未载入');
      return;
    }
    setAgentToolScope(normalizeToolScope(tools));
  } else {
    // 「添加」卡：底稿就是当前勾选的快照（浅拷贝，之后勾选怎么变都不回头改这份底稿）。
    setAgentToolScope(normalizeToolScope([...state.agentFormToolScope]));
  }
  const nameInput = $('#agentToolSetName');
  // 命名栏只预填「被点的那张卡」的名字；「添加」卡没有名字，留空（保存时自动命名）。
  if (nameInput) nameInput.value = preset ? preset.name : (template ? template.name : '');
  enterToolEditorView();
  // 上面 renderToolScopeList → syncAgentToolCheckboxes → updateToolPresetUI 会重绘卡片
  // （编辑态还没显示），所以「按下」状态要在重绘之后再按 key 找回卡片打上。
  markPickedToolCard({ presetId, templateId });
  nameInput?.focus();
  nameInput?.select();
}

// 进入编辑态的共同收尾：清搜索词、按当前模式重画列表、滑入编辑区。
// 从卡片进入 = 分组视图（要看全量、要动手改）；「只看已选」由开关单独切换。
function enterToolEditorView({ onlySelected = false } = {}) {
  state.agentToolFilter = '';
  state.agentToolOnlySelected = onlySelected;
  const filter = $('#agentToolFilter');
  if (filter) filter.value = '';
  renderToolScopeList();
  swapToolView(true);
}

// 给刚点的那张卡打上「按下 / 已选」状态（按 key 重新查，兼容重绘后的新节点）。
function markPickedToolCard({ presetId = '', templateId = '' }) {
  const selector = presetId
    ? `[data-tool-preset-card="${presetId}"]`
    : (templateId ? `[data-tool-template-card="${templateId}"]` : '[data-tool-preset-add]');
  const card = document.querySelector(selector);
  if (!card) return;
  card.classList.add('is-picked');
  card.setAttribute('aria-pressed', 'true');
}

// 「编辑当前工具集」：带着当前勾选直接进编辑态，一个工具都不动。
// 与点卡片的本质区别——不调用 setAgentToolScope（点任何卡片都会用那张卡的工具覆盖当前勾选）。
// 常驻入口：自定义组合匹配不上任何卡片时，它是唯一能改这套勾选的地方；匹配到卡 / 未限制时
// 点它也无害（不改勾选，比点卡片更安全）。命名栏留空 = 保存时另存一套新的「我的工具集」。
export function openAgentToolEditorCurrent() {
  state.agentToolEditingId = '';
  const nameInput = $('#agentToolSetName');
  if (nameInput) nameInput.value = '';
  enterToolEditorView();
  nameInput?.focus();
}

// 当前勾选是不是「还没存成卡片的自定义组合」——点任何卡片都会把它整份换掉的那种。
// 未限制（空表）与已命中某张卡片的组合点卡无损（后者载入的就是同一套），都不算。
function isUnsavedCustomScope() {
  const unrestricted = state.agentFormUnrestricted && !state.agentFormScopeTouched;
  return (state.agentFormToolScope || []).length > 0 && !unrestricted && !matchToolPreset();
}

// 点卡片进编辑态前的防误触：只有「自定义未保存」才拦一次，取消 = 留在卡片态、勾选分毫不动。
// 「添加」卡以当前勾选为底稿、不覆盖任何东西，所以不经过这里（见 handleAgentToolPresetCardsClick）。
export function confirmToolScopeOverwrite() {
  if (!isUnsavedCustomScope()) return true;
  return confirm('当前的自定义工具组合还没有保存成工具集卡片，套用其它工具集会替换现在勾选的全部工具。继续吗？');
}

export function closeAgentToolEditor() {
  state.agentToolEditingId = '';
  // 回到卡片态：下次点卡片重新从分组视图开始（「只看已选」只在当前这次编辑里生效）。
  state.agentToolOnlySelected = false;
  swapToolView(false);
  resetAgentToolPeek();
  renderAgentToolPresetCards();
}

// 保存工具集：写入后端「我的工具集」（编辑中的原地更新），当前勾选已经是表单里的值，无需再套用。
export async function saveAgentToolSet() {
  const known = knownToolNames();
  const tools = normalizeToolMutex(state.agentFormToolScope.filter((name) => known.has(name)));
  if (!tools.length) {
    toast('还没勾选任何工具，先选几个再保存');
    return;
  }
  const nameInput = $('#agentToolSetName');
  const raw = String(nameInput?.value || '').trim();
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const name = raw || `自定义组合 ${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const editingId = String(state.agentToolEditingId || '');
  try {
    await api('/api/tool_sets', {
      method: 'POST',
      body: { id: editingId, name, tools },
    });
  } catch (error) {
    toast(`保存工具集失败：${error.message}`);
    return;
  }
  await refreshToolTemplates();
  if (nameInput) nameInput.value = '';
  closeAgentToolEditor();
  toast(editingId ? `已更新工具集「${name}」` : `已保存工具集「${name}」`);
}

// 卡片区事件委托：× 删除优先；其余任意卡片（预设 / 我的工具集 / 添加）都进入编辑态，
// 命名栏预填该卡片的名字（「添加」卡留空），工具列表供查看与编辑；被点的卡会先「按下」。
export function handleAgentToolPresetCardsClick(event) {
  const del = event.target.closest('[data-tool-template-del]');
  if (del) {
    event.stopPropagation();
    void deleteToolTemplate(del.dataset.toolTemplateDel);
    return;
  }
  const add = event.target.closest('[data-tool-preset-add]');
  if (add) {
    openAgentToolEditor({});
    return;
  }
  // 防误触：预设卡 / 「我的工具集」卡都会用那张卡的工具覆盖当前勾选，
  // 只在这份勾选是「还没存成卡片的自定义组合」时才问一次（取消 = 分毫不动）。
  if (!confirmToolScopeOverwrite()) return;
  const template = event.target.closest('[data-tool-template-card]');
  if (template) {
    openAgentToolEditor({ templateId: template.dataset.toolTemplateCard });
    return;
  }
  const preset = event.target.closest('[data-tool-preset-card]');
  if (preset) openAgentToolEditor({ presetId: preset.dataset.toolPresetCard });
}

// 卡片是 div[role=button]：Enter/Space 等同点击。
export function handleAgentToolPresetCardsKeydown(event) {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const card = event.target.closest('.tool-preset-card');
  if (!card) return;
  event.preventDefault();
  card.click();
}

// —— 我的工具集：把任意自定义组合存成命名工具集，之后在别的 Agent 表单里点一下卡片即可套用。
// 存后端（config.json 的 tool_sets），**不再用 localStorage**：冻结版 pywebview 默认
// private_mode=True，WebView2 的 localStorage 每次退出都会被清空，用户保存的工具集会凭空消失。
// 老版本存在 localStorage 的数据由 migrateLegacyToolTemplates() 一次性搬到后端。

export const TOOL_TEMPLATE_STORE = 'naiba.agentToolTemplates';
let toolTemplatesMigrated = false;

export function loadToolTemplates() {
  return Array.isArray(state.toolTemplates) ? state.toolTemplates : [];
}

// 老数据（localStorage 时代）一次性搬到后端：只在后端还没有任何工具集时执行，搬完删掉旧键。
export async function migrateLegacyToolTemplates() {
  if (toolTemplatesMigrated) return;
  toolTemplatesMigrated = true;
  let legacy = [];
  try {
    const raw = JSON.parse(localStorage.getItem(TOOL_TEMPLATE_STORE) || '[]');
    legacy = Array.isArray(raw) ? raw : [];
  } catch (_error) {
    legacy = [];
  }
  if (!legacy.length) return;
  if (loadToolTemplates().length) {
    try { localStorage.removeItem(TOOL_TEMPLATE_STORE); } catch (_error) { /* 忽略 */ }
    return;
  }
  for (const item of legacy) {
    const tools = Array.isArray(item?.tools) ? item.tools : [];
    if (!tools.length) continue;
    try {
      await api('/api/tool_sets', {
        method: 'POST',
        body: { name: String(item.name || ''), tools },
      });
    } catch (error) {
      toast(`旧工具集「${item.name || ''}」迁移失败：${error.message}`);
    }
  }
  try { localStorage.removeItem(TOOL_TEMPLATE_STORE); } catch (_error) { /* 忽略 */ }
  await refreshToolTemplates();
  renderAgentToolPresetCards();
}

// 从后端拉一次最新列表（保存/删除后调用，保证多端一致）。
export async function refreshToolTemplates() {
  try {
    const data = await api('/api/tool_sets');
    state.toolTemplates = Array.isArray(data?.tool_sets) ? data.tool_sets : [];
  } catch (error) {
    toast(`工具集列表读取失败：${error.message}`);
  }
  return state.toolTemplates;
}

export function knownToolNames() {
  return new Set((state.toolCatalog?.tools || []).map((tool) => tool.name));
}

// 模板里可能存过已被移除的工具名：复刻时只应用当前目录里还存在的；
// 再过一遍互斥归一，防止老数据/手攒的模板把互斥的两个子代理工具都存进来。
export function usableTemplateTools(template) {
  const known = knownToolNames();
  return normalizeToolMutex((template.tools || []).filter((name) => known.has(name)));
}

// 这套「我的工具集」当前被哪些 Agent 用着：与 matchToolScope 同口径（都过 matchableTools 滤幽灵名后
// 做集合全等），未限制（空 tool_scope）不引用任何工具集、直接跳过。删除前把「谁在用」摆给用户看。
export function toolTemplateUsedByAgents(template, agents) {
  const usable = usableTemplateTools(template);
  if (!usable.length) return [];
  const same = new Set(usable);
  return (agents || [])
    .filter((agent) => {
      const tools = matchableTools(agent?.tool_scope);
      return tools.length > 0 && tools.length === same.size && tools.every((name) => same.has(name));
    })
    .map((agent) => ({ id: agent.id, name: agent.name || agent.id }));
}

export async function deleteToolTemplate(templateId) {
  const template = loadToolTemplates().find((item) => item.id === templateId);
  if (!template) return;
  // 删除不可逆，值得先拉一次最新 Agent 列表再判定「谁在用」：内存里的 state 可能停在别处改过之前。
  // 拉不到就用现有列表兜底，不阻断删除。
  try {
    await refreshAgentsFromServer();
  } catch (_error) { /* 忽略：引用提示是辅助信息，不该拦住删除 */ }
  const users = toolTemplateUsedByAgents(template, state.bootstrap?.agents || []);
  const lines = users.length
    ? `\n\n以下 ${users.length} 个 Agent 当前套用的正是这套工具：\n${users.map((agent) => `· ${agent.name}`).join('\n')}`
      + '\n\n删除只移除这张工具集卡片，不会改动这些 Agent 已保存的配置。'
    : '';
  if (!confirm(`确定删除工具集「${template.name}」吗？${lines}`)) return;
  try {
    await api(`/api/tool_sets/${encodeURIComponent(templateId)}`, { method: 'DELETE' });
  } catch (error) {
    toast(`删除工具集失败：${error.message}`);
    return;
  }
  await refreshToolTemplates();
  renderAgentToolPresetCards();
  toast('工具集已删除');
}

// 用户主动改动工具集时才走这里：标记 touched，并结束“不限制”状态
// （一旦手动选过，就按显式列表保存，不再退回空数组语义）。
export function setAgentToolScope(next) {
  state.agentFormToolScope = next;
  state.agentFormScopeTouched = true;
  state.agentFormUnrestricted = false;
}

// 旧配置里“当前未注册”的工具：保留并告知用户，可一键清除。
export function renderUnknownToolsHint() {
  const box = $('#agentToolUnknownHint');
  if (!box) return;
  const unknown = state.agentFormUnknownTools || [];
  if (!unknown.length) {
    box.hidden = true;
    box.textContent = '';
    return;
  }
  box.hidden = false;
  box.textContent = `旧配置里有 ${unknown.length} 个当前未注册的工具（${unknown.join('、')}），已原样保留，不影响使用。`;
  let btn = box.querySelector('button');
  if (!btn) {
    btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'control-button tiny';
    btn.textContent = '清除';
    btn.addEventListener('click', () => {
      state.agentFormUnknownTools = [];
      state.agentFormScopeTouched = true;
      renderUnknownToolsHint();
    });
    box.append(btn);
  }
}

export function setToolGroupCollapsed(groupEl, collapsed) {
  if (!groupEl) return;
  groupEl.classList.toggle('collapsed', collapsed);
  // 展开区统一包在 .agent-tool-group-body 里（单层网格，或"平铺 + MCP 二级分组"两种形态）。
  const body = groupEl.querySelector('.agent-tool-group-body');
  if (body) body.hidden = collapsed;
}

export function toggleToolGroup(groupEl) {
  setToolGroupCollapsed(groupEl, !groupEl.classList.contains('collapsed'));
}

// 二级分组（当前用于 MCP：按服务器聚合）的全选框与计数，口径与分类级完全一致。
function updateSubgroupSelectAll(subEl) {
  const all = subEl.querySelector('input.subgroup-select-all');
  if (!all) return;
  const cbs = [...subEl.querySelectorAll('.permission-grid input[type="checkbox"]')];
  const selected = cbs.filter((cb) => state.agentFormToolScope.includes(cb.value));
  all.checked = cbs.length > 0 && selected.length === cbs.length;
  all.indeterminate = cbs.length > 0 && selected.length > 0 && selected.length < cbs.length;
  const count = subEl.querySelector('.subgroup-count');
  if (count) count.textContent = `${selected.length}/${cbs.length}`;
}

export function updateGroupSelectAll(groupEl) {
  if (!groupEl) return;
  const all = groupEl.querySelector('input.group-select-all');
  if (all) {
    const toolCbs = [...groupEl.querySelectorAll('.permission-grid input[type="checkbox"]')];
    const selected = toolCbs.filter((cb) => state.agentFormToolScope.includes(cb.value));
    all.checked = toolCbs.length > 0 && selected.length === toolCbs.length;
    // 半选态：该分类下只有部分工具被勾选。
    all.indeterminate = toolCbs.length > 0 && selected.length > 0 && selected.length < toolCbs.length;
    const count = groupEl.querySelector('.group-count');
    if (count) count.textContent = `${selected.length}/${toolCbs.length}`;
  }
  groupEl.querySelectorAll('.tool-subgroup').forEach(updateSubgroupSelectAll);
}

export function syncAgentToolCheckboxes(list) {
  if (!list) return;
  list.querySelectorAll('.permission-grid input[type="checkbox"]').forEach((cb) => {
    cb.checked = state.agentFormToolScope.includes(cb.value);
  });
  // 同步各分类的“全选”框状态（含半选）与计数。
  list.querySelectorAll('.agent-tool-group').forEach(updateGroupSelectAll);
  updateToolCounter();
  updateToolPresetUI();
}

// 工具目录（/api/tool_catalog）带**短时效缓存**：MCP 是「按需连接」的——应用刚启动时
// 服务还没连上，此时拉到的目录里没有 mcp__* 工具；若把它永久缓存，整个页面会话的
// 工具集面板就再也看不到 MCP 工具（用户实测「MCP 工具消失了」）。
// 所以：默认缓存 5 秒，打开工具集面板时强制取新（maxAgeMs: 0），MCP 连接状态变化时清缓存。
let toolCatalogPromise = null;

export async function ensureToolCatalog({ maxAgeMs = 5000 } = {}) {
  const cachedAt = Number(state.toolCatalogAt || 0);
  if (state.toolCatalog && Date.now() - cachedAt < maxAgeMs) return state.toolCatalog;
  if (!toolCatalogPromise) {
    toolCatalogPromise = api('/api/tool_catalog', { method: 'GET' })
      .then((data) => {
        if (data) {
          state.toolCatalog = data;
          state.toolCatalogAt = Date.now();
        }
        return data;
      })
      .catch(() => null)
      .finally(() => { toolCatalogPromise = null; });
  }
  await toolCatalogPromise;
  return state.toolCatalog || null;
}

// MCP 连接状态变化 → 工具目录作废（下次渲染/打开面板会重新拉，MCP 工具立即回来）。
export function invalidateToolCatalog() {
  state.toolCatalog = null;
  state.toolCatalogAt = 0;
  toolCatalogPromise = null;
}

export async function renderAgentToolPicker() {
  const list = $('#agentToolScope');
  if (!list) return;
  // 打开工具集面板时强制取新：MCP 工具是运行时按需连接的，用启动时的旧目录会「看不见 MCP 工具」。
  await ensureToolCatalog({ maxAgeMs: 0 });
  const catalog = state.toolCatalog?.tools || [];
  // 兼容旧配置：挑出当前工具目录里已不存在的名字（老版本移除的工具、临时掉线的
  // MCP 工具等）单独保留。它们不参与勾选、计数与预设匹配，但保存时原样写回，
  // 这样旧 Agent 打开就能看到原本的勾选，不用重新配一遍。
  if (state.agentFormToolScope.length) {
    const knownNames = new Set(catalog.map((tool) => tool.name));
    state.agentFormUnknownTools = [...new Set(
      state.agentFormToolScope.filter((name) => !knownNames.has(name)),
    )];
    state.agentFormToolScope = state.agentFormToolScope.filter((name) => knownNames.has(name));
  }
  // 仍未选择时：新 Agent 用后端默认选中集（=标准模式）；旧 Agent（空 tool_scope=不限制）按全选展示。
  if (!state.agentFormToolScope.length) {
    state.agentFormToolScope = state.agentFormIsNew
      ? catalog.filter((t) => t.default_selected).map((t) => t.name)
      : catalog.map((t) => t.name);
  }
  // 初始加载也应用依赖联动，让显示状态与运行时放行的 allowed_tools 一致。
  // 这里不置 touched：自动补依赖不算用户改配置，未限制的旧 Agent 仍按“不限制”保存。
  state.agentFormToolScope = normalizeToolScope(state.agentFormToolScope);
  // 每次打开表单都回到卡片态（编辑中的工具集不跨表单残留），清单回到展开态、开关回到分组视图。
  state.agentToolEditingId = '';
  state.agentToolOnlySelected = false;
  swapToolView(false);
  resetAgentToolPeek();
  renderAgentToolPresetCards();
  renderUnknownToolsHint();
  renderToolScopeList();
}

// 工具卡片（分组视图与搜索结果共用）：勾选框 + 名称 + 两行说明。
function buildToolCard(tool, list) {
  const label = document.createElement('label');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.value = tool.name;
  cb.checked = state.agentFormToolScope.includes(tool.name);
  if (tool.model_target === 'vision') {
    cb.title = '针对支持看图的视觉模型（多模态大脑）：直接读取图片。';
  } else if (tool.model_target === 'text') {
    cb.title = '针对文本模型：通过视觉车道解读图片。';
  }
  cb.addEventListener('change', (e) => {
    setAgentToolScope(applyAgentToolDependency(
      state.agentFormToolScope, tool.name, e.target.checked,
    ));
    syncAgentToolCheckboxes(list);
    queueSelectedScopeRerender();
  });
  const span = document.createElement('span');
  const b = document.createElement('b');
  b.textContent = tool.name;
  if (AGENT_TOOL_DEP_RULES[tool.name]) {
    b.title = '选中后会自动带上其依赖的查询工具（job_output/job_status/job_wait/job_kill 等）。';
  } else if (MUTEX_RIVALS.has(tool.name)) {
    b.title = '与同组的另一个子 Agent 工具互斥：勾选它会自动取消那个（一次只能开一个模式）。';
  }
  const small = document.createElement('small');
  small.textContent = tool.description || '';
  // 卡片里说明只显示两行（保持紧凑、行高一致），完整说明放 title 悬停查看。
  if (tool.description) label.title = `${tool.name}：${tool.description}`;
  span.append(b, small);
  label.append(cb, span);
  return label;
}

function buildToolGrid(tools, list) {
  const grid = document.createElement('div');
  grid.className = 'permission-grid';
  for (const tool of tools) grid.append(buildToolCard(tool, list));
  return grid;
}

// 二级分组（当前用于 MCP：按服务器聚合）：服务器名 + 该服务器的全选框与计数 + 工具网格。
// 这样 40 个 MCP 工具按服务器分块，可以整块全选，不用逐个点。
function buildSubgroupBlock(sub, tools, list) {
  const block = document.createElement('div');
  block.className = 'tool-subgroup';
  const head = document.createElement('div');
  head.className = 'tool-subgroup-head';
  const all = document.createElement('input');
  all.type = 'checkbox';
  all.className = 'subgroup-select-all';
  all.title = `全选/取消全选「${sub.name}」服务器下的所有工具`;
  all.addEventListener('click', (e) => e.stopPropagation());
  all.addEventListener('change', () => {
    let scope = state.agentFormToolScope;
    for (const tool of tools) {
      // 批量勾选不带"想选哪个"的语义 ⇒ 关掉逐项互斥，末尾统一按组内排前者归一。
      scope = applyAgentToolDependency(scope, tool.name, all.checked, { applyMutex: false });
    }
    const before = scope;
    scope = normalizeToolMutex(scope);
    if (all.checked) notifyToolMutexDrop(before, scope);
    setAgentToolScope(scope);
    syncAgentToolCheckboxes(list);
  });
  const name = document.createElement('span');
  name.className = 'subgroup-title';
  name.textContent = sub.name;
  const count = document.createElement('span');
  count.className = 'subgroup-count';
  head.append(all, name, count);
  block.append(head, buildToolGrid(tools, list));
  return block;
}

function buildGroupBlock(group, toolMap, list, { onlySelected = false } = {}) {
  const selected = new Set(state.agentFormToolScope);
  // 「只看已选」视图：网格里只放已勾选的工具（keep 负责过滤，空分类由调用方跳过）。
  const keep = (names) => (onlySelected
    ? (names || []).filter((name) => selected.has(name) && toolMap.has(name))
    : (names || []));
  const groupEl = document.createElement('div');
  // 只看已选时必须展开：折叠起来又变成「不知道配了什么」，正是本次要修的问题。
  groupEl.className = onlySelected ? 'agent-tool-group is-only-selected' : 'agent-tool-group collapsed';
  groupEl.dataset.group = group.name;

  const head = document.createElement('div');
  head.className = 'agent-tool-group-head';
  head.setAttribute('role', 'button');
  head.tabIndex = 0;
  head.title = '点击展开/收起，展开后可逐个勾选';

  const caret = document.createElement('span');
  caret.className = 'group-caret';
  caret.textContent = '▸';

  // 分类级“全选”：点击一次勾选/取消该分类所有工具（沿用依赖联动）。
  const allCb = document.createElement('input');
  allCb.type = 'checkbox';
  allCb.className = 'group-select-all';
  allCb.setAttribute('data-group', group.name);
  allCb.title = onlySelected
    ? `取消勾选「${group.name}」分类下已选的工具`
    : `全选/取消全选「${group.name}」分类下的所有工具`;
  allCb.addEventListener('click', (e) => e.stopPropagation());
  allCb.addEventListener('change', () => {
    // 只看已选视图里网格只有已勾选的工具：取消全选框 = 清空该分类的已选。
    // 绝不能走下面的「整组全选」分支 —— 那会让用户在这一视图里凭空多选出一批没勾过的工具。
    if (onlySelected) {
      if (!allCb.checked) {
        const groupTools = new Set(group.tools || []);
        setAgentToolScope(state.agentFormToolScope.filter((name) => !groupTools.has(name)));
        queueSelectedScopeRerender();
      }
      syncAgentToolCheckboxes(list);
      return;
    }
    let scope = state.agentFormToolScope;
    for (const name of group.tools || []) {
      const tool = toolMap.get(name);
      // 全选没有"用户想选哪个互斥项"的语义 ⇒ 关掉逐项互斥，末尾按组内排前者归一。
      if (tool) scope = applyAgentToolDependency(scope, tool.name, allCb.checked, { applyMutex: false });
    }
    const before = scope;
    scope = normalizeToolMutex(scope);
    if (allCb.checked) notifyToolMutexDrop(before, scope);
    setAgentToolScope(scope);
    // 勾上分类时自动展开，让用户看到自己到底开了什么。
    if (allCb.checked) setToolGroupCollapsed(groupEl, false);
    syncAgentToolCheckboxes(list);
  });

  const title = document.createElement('span');
  title.className = 'group-title';
  title.textContent = group.name;
  // 风险徽标（只读 / 会改文件 / 高风险 / 联网 / 会写产物 / 会改动）：配色走 data-tone。
  if (group.badge) {
    const badge = document.createElement('em');
    badge.className = 'group-badge';
    badge.dataset.tone = group.tone || 'info';
    badge.textContent = group.badge;
    title.append(badge);
  }
  const count = document.createElement('span');
  count.className = 'group-count';
  const desc = document.createElement('small');
  desc.className = 'group-desc';
  desc.textContent = group.desc || '';
  // 一行顺序：箭头 · 全选框 · 分类名（含徽标）· 小字说明 · 计数（CSS 按此列序排布）
  head.append(caret, allCb, title, desc, count);
  head.addEventListener('click', () => toggleToolGroup(groupEl));
  head.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggleToolGroup(groupEl);
    }
  });

  // 展开区：先平铺"不属于二级分组"的工具，再逐个渲染二级分组（MCP 按服务器）。
  const body = document.createElement('div');
  body.className = 'agent-tool-group-body';
  body.hidden = !onlySelected;
  const direct = keep(group.direct_tools || group.tools || [])
    .map((name) => toolMap.get(name)).filter(Boolean);
  if (direct.length) body.append(buildToolGrid(direct, list));
  for (const sub of group.subgroups || []) {
    const tools = keep(sub.tools).map((name) => toolMap.get(name)).filter(Boolean);
    if (!tools.length) continue;
    body.append(buildSubgroupBlock(sub, tools, list));
  }
  groupEl.append(head, body);
  return groupEl;
}

function emptyScopeHint(text) {
  const p = document.createElement('p');
  p.className = 'tool-scope-empty';
  p.textContent = text;
  return p;
}

// 分组视图：6 个分类；MCP 动态工具在「联网与外部服务」内按服务器二级分组。
function renderGroupedScope(list, groups, toolMap) {
  let rendered = 0;
  for (const group of groups) {
    if (!(group.tools || []).some((name) => toolMap.has(name))) continue;
    list.append(buildGroupBlock(group, toolMap, list));
    rendered += 1;
  }
  if (!rendered) list.append(emptyScopeHint('工具目录为空'));
}

// 「只看已选」视图：按分类摊开当前勾选的工具，空分类直接不出现。
// 已配好的 Agent 再次打开时，分组视图里 6 个分类全折叠、只给「已选 x/y」，
// 用户看不出这一套到底开了什么（本次要修的问题）；这个视图把答案直接摊开。
function renderSelectedScope(list, groups, toolMap) {
  const selected = new Set(state.agentFormToolScope);
  let rendered = 0;
  for (const group of groups) {
    if (!(group.tools || []).some((name) => selected.has(name) && toolMap.has(name))) continue;
    list.append(buildGroupBlock(group, toolMap, list, { onlySelected: true }));
    rendered += 1;
  }
  if (!rendered) {
    list.append(emptyScopeHint('当前没有勾选任何工具。再点一次「只看已选」回到分组视图。'));
  }
}

// 「只看已选」视图下列表内容就是「已选集合」本身：勾选一变就得重画（取消的卡片要立刻消失）。
// 延到下一帧，避免在事件处理途中把当前节点换掉（正被点击的那张卡片还在冒泡）。
function queueSelectedScopeRerender() {
  if (!state.agentToolOnlySelected) return;
  window.requestAnimationFrame(() => {
    if (state.agentToolOnlySelected) renderToolScopeList();
  });
}

// 搜索结果视图：命中工具平铺一层（卡片带所属分类标签），省去在分组里逐层展开找。
function renderFilteredScope(list, catalog, filter) {
  const matched = catalog.filter((tool) => (
    `${tool.name} ${tool.description || ''}`.toLowerCase().includes(filter)
  ));
  if (!matched.length) {
    list.append(emptyScopeHint(`没有匹配「${String(state.agentToolFilter).trim()}」的工具`));
    return;
  }
  const groupOf = new Map();
  for (const group of state.toolCatalog?.groups || []) {
    for (const name of group.tools || []) groupOf.set(name, group.name);
  }
  const box = document.createElement('div');
  box.className = 'agent-tool-group';
  const head = document.createElement('div');
  head.className = 'agent-tool-group-head search-head';
  const title = document.createElement('span');
  title.className = 'group-title';
  title.textContent = '搜索结果';
  const tip = document.createElement('small');
  tip.className = 'group-desc';
  tip.textContent = '清空搜索框即恢复分组视图';
  const count = document.createElement('span');
  count.className = 'group-count';
  count.textContent = `${matched.length} 个`;
  head.append(title, tip, count);
  const body = document.createElement('div');
  body.className = 'agent-tool-group-body';
  const grid = buildToolGrid(matched, list);
  // 卡片上标出所属分类，避免"只看到工具名、不知道它属于哪一组"。
  [...grid.children].forEach((card, index) => {
    const tag = document.createElement('em');
    tag.className = 'tool-group-tag';
    tag.textContent = groupOf.get(matched[index].name) || '';
    card.querySelector('span')?.append(tag);
  });
  body.append(grid);
  box.append(head, body);
  list.append(box);
}

// 只重画工具列表（搜索框输入时调用）：不重拉目录、不改已选范围。
export function renderToolScopeList() {
  const list = $('#agentToolScope');
  if (!list) return;
  const catalog = state.toolCatalog?.tools || [];
  const groups = state.toolCatalog?.groups || [];
  const toolMap = new Map(catalog.map((tool) => [tool.name, tool]));
  const filter = String(state.agentToolFilter || '').trim().toLowerCase();
  list.innerHTML = '';
  // 「只看已选」优先于搜索：它是一份核对清单，搜索框此时不参与（切换开关时会清空搜索词）。
  if (state.agentToolOnlySelected) renderSelectedScope(list, groups, toolMap);
  else if (filter) renderFilteredScope(list, catalog, filter);
  else renderGroupedScope(list, groups, toolMap);
  // 初始渲染后同步一次，让各分类“全选”框进入正确的勾选/半选状态。
  syncAgentToolCheckboxes(list);
}

// 「展开全部 / 收起全部」：一键切换所有分类。
export function toggleAllToolGroups() {
  const list = $('#agentToolScope');
  if (!list) return;
  const groupEls = [...list.querySelectorAll('.agent-tool-group')];
  const expand = groupEls.some((el) => el.classList.contains('collapsed'));
  groupEls.forEach((el) => setToolGroupCollapsed(el, !expand));
  const btn = $('#toggleAllToolGroups');
  if (btn) btn.textContent = expand ? '收起全部' : '展开全部';
}

// 关闭 Agent 弹层（Esc、右上角关闭、取消、保存成功四条路径都走这里）。
export function hideAgentForm() {
  const dialog = $('#agentDialog');
  if (dialog?.open) dialog.close();
  $('#agentError').textContent = '';
}

export async function saveAgentForm() {
  // 旧 Agent 若原本是空 tool_scope（=不限制）且用户没动过勾选，就继续以空数组保存，
  // 保留“以后新增工具自动纳入”的语义，不要在这里被固化成一份死的工具名单。
  const keepUnrestricted = state.agentFormUnrestricted && !state.agentFormScopeTouched;
  const payload = {
    // 新建时不带 id（留空）：后端分配持久化唯一 id，前端不再让用户手填。
    id: $('#agentFormId').value.trim(),
    name: $('#agentName').value.trim(),
    system_prompt: $('#agentSystemPromptEdit').value,
    skill_ids: state.agentFormSkillIds,
    tool_scope: keepUnrestricted
      ? []
      : [...new Set([...state.agentFormToolScope, ...state.agentFormUnknownTools])],
  };
  try {
    const saved = await api('/api/agents', { method: 'POST', body: payload });
    // 头像延后到这里上传：新建 Agent 保存前还没有 id。
    if (agentAvatarFile && saved?.id) {
      try {
        const form = new FormData();
        form.append('agent_id', saved.id);
        form.append('file', agentAvatarFile, agentAvatarFile.name || 'avatar.png');
        await api('/api/agents/avatar', { method: 'POST', body: form });
      } catch (error) {
        toast(`头像上传失败：${error.message}`);
      }
      agentAvatarFile = null;
    }
    await refreshAgentsFromServer();
    hideAgentForm();
    renderAgents();
    renderAgentManager();
    applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
    toast('Agent 已保存');
  } catch (error) {
    $('#agentError').textContent = error.message;
  }
}

export async function deleteAgent(agentId) {
  const agent = (state.bootstrap?.agents || []).find((item) => item.id === agentId);
  if (!confirm(`删除 Agent「${agent?.name || agentId}」？引用它的对话将回退到默认 Agent。`)) return;
  try {
    await api(`/api/agents/${encodeURIComponent(agentId)}`, { method: 'DELETE' });
    await refreshAgentsFromServer();
    // 正在编辑被删掉的 Agent：连弹层一起关掉，避免表单停在已不存在的条目上。
    if ($('#agentFormId').value === agentId) hideAgentForm();
    renderAgents();
    renderAgentManager();
    applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
    toast('Agent 已删除');
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

export async function saveRuntimeSettings() {
  const mode = [...document.querySelectorAll('input[name="proxyMode"]')].find((el) => el.checked)?.value || 'system';
  let proxy;
  if (mode === 'manual') {
    const url = String($('#proxyUrl')?.value || '').trim();
    if (!url) {
      toast('手动代理需要填写代理地址（如 http://127.0.0.1:7890）');
      return;
    }
    proxy = { enabled: true, url, use_system_fallback: false };
  } else if (mode === 'direct') {
    proxy = { enabled: false, url: '', use_system_fallback: false };
  } else {
    proxy = { enabled: true, url: '', use_system_fallback: true };
  }
  // 阈值留空按默认 80 处理（0 才是"关闭提醒"，避免误清空导致静默关闭）。
  const warningRaw = String($('#contextWarningPercent')?.value ?? '').trim();
  // 这两项同理：0 是有效值，只有「留空」才回落到默认值。
  const firstByteRaw = String($('#localFirstByteTimeout')?.value ?? '').trim();
  const maxStepsRaw = String($('#agentStepLimit')?.value ?? '').trim();
  const replayMaxRaw = String($('#reasoningReplayMaxChars')?.value ?? '').trim();
  const replayTurnRaw = String($('#reasoningReplayTurnChars')?.value ?? '').trim();
  const imageCacheRaw = String($('#imageEncodeCacheMb')?.value ?? '').trim();
  const payload = {
    command_timeout: Number($('#commandTimeout')?.value || 120),
    context_warning_percent: warningRaw === '' ? 80 : Number(warningRaw),
    local_first_byte_timeout_seconds: firstByteRaw === '' ? 120 : Number(firstByteRaw),
    // 思考回放限长：0 有意义（关闭限长），只有「留空」才回落到默认值。
    reasoning_replay_max_chars: replayMaxRaw === '' ? 4000 : Number(replayMaxRaw),
    reasoning_replay_turn_chars: replayTurnRaw === '' ? 16000 : Number(replayTurnRaw),
    // 图片编码记忆：0 = 关闭记忆（每轮重编码），同样只有「留空」才回落默认值。
    image_encode_cache_mb: imageCacheRaw === '' ? 512 : Number(imageCacheRaw),
    agent_step_limit: maxStepsRaw === '' ? 200 : Number(maxStepsRaw),
    context_reset_seed_template: String($('#contextResetSeedTemplate')?.value || ''),
    workspace_dir: $('#workspaceDir')?.value.trim() || '',
    imaging: {
      image_upload_original: Boolean($('#imageUploadOriginal')?.checked),
      image_max_pixels: Number($('#imageMaxPixels')?.value || 2000000),
      thumbnail_max_pixels: Number($('#thumbnailMaxPixels')?.value || 500000),
      auto_clean_limit_mb: Number($('#autoCleanLimitMb')?.value ?? 256),
      generated_clean_limit_mb: Number($('#generatedCleanLimitMb')?.value ?? 512),
    },
    proxy,
  };
  const result = await api('/api/settings', { method: 'POST', body: payload });
  Object.assign(state.bootstrap.settings, result.settings);
  state.bootstrap.resolved_workspace_dir = result.resolved_workspace_dir || state.bootstrap.resolved_workspace_dir;
  if ($('#resolvedWorkspaceDir')) $('#resolvedWorkspaceDir').textContent = state.bootstrap.resolved_workspace_dir || '-';
  if (result.image_cache_bytes !== undefined) renderCacheSizes(result);
  if (result.proxy_state) {
    state.bootstrap.proxy_state = result.proxy_state;
    renderProxyStateHint(result);
  }
  renderWorkspaceControl();
  toast('运行参数已保存');
}

export async function saveWorkspaceSettings() {
  const value = $('#workspaceDialogInput')?.value.trim() || '';
  try {
    const result = await api('/api/settings', { method: 'POST', body: { workspace_dir: value } });
    Object.assign(state.bootstrap.settings, result.settings);
    state.bootstrap.resolved_workspace_dir = result.resolved_workspace_dir || state.bootstrap.resolved_workspace_dir;
    state.workspaceBrowsePath = state.bootstrap.resolved_workspace_dir;
    if ($('#workspaceDir')) $('#workspaceDir').value = value;
    renderWorkspaceControl();
    if ($('#workspaceDialog')) $('#workspaceDialog').close();
    toast('工作区已保存');
  } catch (error) {
    toast(`工作区保存失败：${error.message}`);
  }
}

export async function pickWorkspace(targetId = 'workspaceDialogInput') {
  try {
    const current = String($(targetId)?.value || '');
    const result = await api('/api/workspace/pick', { method: 'POST', body: { initial: current } });
    if (result.cancelled) return;
    const input = $('#' + targetId);
    if (input) input.value = result.path || '';
    if (targetId === 'workspaceDir' && $('#resolvedWorkspaceDir')) {
      $('#resolvedWorkspaceDir').textContent = result.resolved || '-';
    }
  } catch (error) { toast(`目录选择失败：${error.message}`); }
}

export async function saveAccessToken() {
  const value = $('#accessTokenInput').value.trim();
  if (!value) {
    toast('请输入新口令');
    return;
  }
  if (value.length < 4) {
    toast('口令至少 4 位');
    return;
  }
  const result = await api('/api/settings', { method: 'POST', body: { access_token: value } });
  Object.assign(state.bootstrap.settings, result.settings);
  // 更新本会话使用的口令，避免保存后立即失效
  state.token = value;
  localStorage.setItem('naibaChatToken', value);
  $('#accessTokenInput').value = '';
  $('#accessTokenInput').placeholder = '口令已更新（输入可再次修改）';
  toast('口令已更新，其他设备需用新口令登录');
}

export function mcpServerState(server) {
  if (server.status === 'error' || server.error) return { text: '错误', color: '#e45e55' };
  if (server.activity === 'calling' || (server.active_calls && server.active_calls > 0)) return { text: '使用中', color: '#3ecf8e' };
  if (server.status === 'connecting' || server.status === 'reconnecting') return { text: '连接中', color: '#e0a13a' };
  if (server.connected) return { text: '已就绪', color: '#3ecf8e' };
  return { text: '待机', color: '#7d867d' };
}

export function renderMcp() {
  const servers = state.bootstrap.mcp_servers || [];
  // 顶栏 MCP 指示灯：**只看颜色**（绿=已连接 / 红=未连接或出错 / 黄=连接中 / 灰=未配置服务），
  // 文字恒为「MCP」，具体状态放 title 里（此前文字拼「MCP · 已就绪」等，啰嗦且占宽）。
  const mcpButton = $('#mcpStatus');
  const label = mcpButton.querySelector('span');
  const anyError = servers.some((s) => s.status === 'error' || s.error);
  const anyCalling = servers.some((s) => s.activity === 'calling' || (s.active_calls && s.active_calls > 0));
  const anyConnecting = servers.some((s) => s.status === 'connecting' || s.status === 'reconnecting');
  const allConnected = servers.length > 0 && servers.every((s) => s.connected);
  mcpButton.classList.remove('connected', 'calling', 'connecting', 'error', 'disconnected');
  let statusText;
  if (anyError) {
    mcpButton.classList.add('error');
    statusText = '连接错误';
  } else if (anyConnecting) {
    mcpButton.classList.add('connecting');
    statusText = '连接中';
  } else if (allConnected) {
    mcpButton.classList.add(anyCalling ? 'calling' : 'connected');
    statusText = anyCalling ? '使用中' : '已连接';
  } else if (servers.length) {
    mcpButton.classList.add('disconnected');
    statusText = '未连接';
  } else {
    statusText = '未配置服务';
  }
  if (label) label.textContent = 'MCP';
  mcpButton.title = `MCP：${statusText}（点击查看连接状态）`;

  $('#mcpList').innerHTML = servers.map((server) => {
    const st = mcpServerState(server);
    const detail = server.connected
      ? `${server.tools?.length ?? 0} 个工具`
      : (server.status === 'idle' ? '仅在本轮激活的 Skill 需要 MCP 时连接' : escapeHtml(server.error || st.text));
    return `<div class="connection-item">
      <span><b>${escapeHtml(server.id)} · ${st.text}</b><small>${detail}</small></span>
      <span class="status-mark" style="background:${st.color}"></span>
    </div>`;
  }).join('') || '<p class="activity">没有注册 MCP 服务</p>';
  $$('#mcpList .connection-item').forEach((item, index) => {
    const server = servers[index];
    if (!server) return;
    const actions = document.createElement('span');
    actions.className = 'mcp-actions';
    const test = document.createElement('button');
    test.className = 'control-button mcp-test';
    test.type = 'button';
    test.textContent = '测试';
    test.addEventListener('click', () => mcpAction(server.id, 'test'));
    const reconnect = document.createElement('button');
    reconnect.className = 'control-button mcp-reconnect';
    reconnect.type = 'button';
    reconnect.textContent = '重连';
    reconnect.addEventListener('click', () => mcpAction(server.id, 'reconnect'));
    const remove = document.createElement('button');
    remove.className = 'control-button mcp-remove';
    remove.type = 'button';
    remove.textContent = '删除';
    remove.addEventListener('click', () => removeMcpServer(server.id));
    actions.append(test, reconnect, remove);
    item.append(actions);
  });
}

export async function mcpAction(serverId, action) {
  try {
    const result = await api('/api/mcp/' + action, { method: 'POST', body: { server_id: serverId } });
    const server = state.bootstrap.mcp_servers.find((item) => item.id === serverId);
    if (server) Object.assign(server, result);
    renderMcp();
    if (action === 'test') {
      const parts = ['MCP 测试完成'];
      if (result.connected !== undefined) parts.push(result.connected ? '已就绪' : '未连接');
      if (result.comfyui_reachable !== undefined) parts.push(result.comfyui_reachable ? 'ComfyUI 可达' : 'ComfyUI 不可达');
      if (result.error) parts.push('错误：' + result.error);
      toast(parts.join(' · '));
    } else {
      toast('MCP 已重新连接');
    }
  } catch (error) {
    toast('MCP 操作失败：' + error.message);
  }
}

export async function removeMcpServer(serverId) {
  if (!confirm(`确定删除 MCP 服务「${serverId}」？`)) return;
  try {
    await api('/api/mcp/remove', { method: 'POST', body: { server_id: serverId } });
    state.bootstrap.mcp_servers = (state.bootstrap.mcp_servers || []).filter((item) => item.id !== serverId);
    renderMcp();
    toast(`MCP 服务「${serverId}」已删除`);
  } catch (error) {
    toast('删除 MCP 服务失败：' + error.message);
  }
}

export async function saveMcpServer() {
  const id = $('#mcpNewId').value.trim();
  const command = $('#mcpNewCommand').value.trim();
  if (!id) { toast('请填写服务 ID'); return; }
  if (!command) { toast('请填写命令（command）'); return; }
  const env = {};
  const comfyBin = $('#mcpNewComfyBin').value.trim();
  if (comfyBin) env.COMFY_BIN = comfyBin;
  const extraRaw = $('#mcpNewEnvJson').value.trim();
  if (extraRaw) {
    let extra;
    try { extra = JSON.parse(extraRaw); }
    catch (_e) { toast('环境变量 JSON 格式不正确'); return; }
    if (!extra || typeof extra !== 'object' || Array.isArray(extra)) { toast('环境变量 JSON 必须是对象'); return; }
    Object.assign(env, extra);
  }
  try {
    await api('/api/mcp/register', { method: 'POST', body: { id, command, args: [], env, enabled: true } });
    $('#mcpNewId').value = '';
    $('#mcpNewCommand').value = '';
    $('#mcpNewComfyBin').value = '';
    $('#mcpNewEnvJson').value = '';
    $('#mcpAddForm').open = false;
    state.bootstrap.mcp_servers = await loadMcpServers();
    renderMcp();
    toast(`MCP 服务「${id}」已注册`);
  } catch (error) {
    toast('注册 MCP 服务失败：' + error.message);
  }
}

// 轻量轮询：仅刷新状态相关字段（status/connected/active_calls/activity/last_used_at），
// 保留 bootstrap 中已有的 tools 与 error 信息，使"使用中/已就绪"状态实时反映。
export async function pollMcpStatus() {
  if (state.mcpPollInFlight || document.visibilityState === 'hidden') return;
  state.mcpPollInFlight = true;
  try {
    const data = await api('/api/mcp/status/light');
    const servers = data.servers || [];
    const prev = state.bootstrap.mcp_servers || [];
    const byId = {};
    for (const s of prev) byId[s.id] = s;
    let connectivityChanged = false;
    for (const s of servers) {
      const cur = byId[s.id];
      if (!cur) {
        // 后端出现了本地快照中不存在的 MCP 服务（例如对话内 agent 刚注册的）。
        // light 接口不含工具明细，升级为全量刷新以完整展示。
        await loadMcpServers();
        return;
      }
      if (Boolean(cur.connected) !== Boolean(s.connected)) connectivityChanged = true;
      cur.status = s.status;
      cur.connected = s.connected;
      cur.active_calls = s.active_calls;
      cur.activity = s.activity;
      cur.last_used_at = s.last_used_at;
    }
    // MCP 刚连上/断开 → 工具目录里 mcp__* 的集合变了，作废缓存，别让工具集面板用旧目录。
    if (connectivityChanged) invalidateToolCatalog();
    renderMcp();
  } catch (_error) {
    /* 轮询失败不阻断界面 */
  } finally {
    state.mcpPollInFlight = false;
  }
}

export function startMcpPoll() {
  if (state.mcpPolling) return;
  state.mcpPolling = true;
  scheduleMcpPoll(2000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      if (state.mcpPollTimer) window.clearTimeout(state.mcpPollTimer);
      state.mcpPollTimer = null;
    } else {
      scheduleMcpPoll(0);
    }
  });
}

export function scheduleMcpPoll(delay = null) {
  if (!state.mcpPolling || document.visibilityState === 'hidden') return;
  if (state.mcpPollTimer) window.clearTimeout(state.mcpPollTimer);
  const servers = state.bootstrap?.mcp_servers || [];
  const active = servers.some((server) => Number(server.active_calls || 0) > 0 || server.status === 'connecting');
  const interval = active ? 2000 : 15000;
  state.mcpPollTimer = window.setTimeout(async () => {
    state.mcpPollTimer = null;
    if (document.visibilityState === 'visible') await pollMcpStatus();
    scheduleMcpPoll();
  }, delay ?? interval);
}

