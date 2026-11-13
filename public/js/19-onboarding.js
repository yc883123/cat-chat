// ============================================================
// 19-onboarding.js —— 首启引导向导：选模板 → 填连接信息 → 检测并选择模型 → 开聊
//
// 触发条件：设置里**一个可用供应商都没有**（既没有本地后端，也没有配过 Key 的在线
// API）。已经能聊的用户永远看不到这个弹层；误跳过了也不亏——设置 → 模型 →「添加 API」
// 弹层里是同一套模板网格与同一份引导文案，引导能力常驻。
//
// 名单与卡片渲染都来自 09-settings.js（名单唯一来源是后端 /api/provider-presets），
// 本模块只负责「向导这个流程」：步骤切换、Key 行、测试、保存、跳过。
// ============================================================

import { $, api, state, toast } from "./01-core.js";
import { fillProviderPresetGuide, loadProviderPresets, markProviderPresetCards, providerPresetById, providerPresets, providerPresetMoreText, providerTestSummary, renderProviderPresetGrid, syncSavedProvider } from "./09-settings.js";

// 跳过状态记 localStorage：冻结版 WebView2 已是持久 profile（launcher.py 里 private_mode=False），
// 侧栏宽度、外观偏好本来就存在这里，不需要为「看过一次引导」再动服务端配置。
export const ONBOARDING_DISMISS_KEY = 'naibaOnboardingDismissed';
// 网格一次露出两行（6 张），其余滚动查看——首屏不把 14 张模板全铺开（「还有 …」那行报名字）。
const ONBOARDING_VISIBLE_PRESETS = 6;

// 向导自己的选中项：不写进 state.providerPresetId，免得和设置弹层的表单来源互相污染。
let selectedPresetId = '';
let modelCheckGeneration = 0;
let modelCheckTimer;
let connectionTestGeneration = 0;
let onboardingGeneration = 0;
let saveInFlight = false;

function invalidateOnboardingChecks() {
  clearTimeout(modelCheckTimer);
  ++modelCheckGeneration;
  ++connectionTestGeneration;
  $('#onboardingLoadModels').disabled = false;
  $('#onboardingLoadModels').textContent = '检测模型';
  $('#onboardingTest').disabled = false;
  $('#onboardingTest').textContent = '测试连接';
}

function setOnboardingModels(models = [], selected = '') {
  const select = $('#onboardingModel');
  const ids = [...new Set(models.map(model => String(typeof model === 'string' ? model : model?.id || '').trim()).filter(Boolean))];
  select.replaceChildren(new Option(ids.length ? '请选择模型' : '检测后选择模型', ''));
  for (const id of ids) select.add(new Option(id, id));
  select.add(new Option('手动填写模型名称…', '__custom__'));
  select.value = selected === '__custom__' || ids.includes(selected) ? selected : ids[0] || '';
  $('#onboardingModelCustomField').hidden = select.value !== '__custom__';
}

export function changeOnboardingModel() {
  ++connectionTestGeneration;
  $('#onboardingTest').disabled = false;
  $('#onboardingTest').textContent = '测试连接';
  $('#onboardingError').textContent = '';
  const custom = $('#onboardingModel').value === '__custom__';
  $('#onboardingModelCustomField').hidden = !custom;
  if (custom) $('#onboardingModelCustom').focus();
}

function onboardingDismissed() {
  try {
    return localStorage.getItem(ONBOARDING_DISMISS_KEY) === '1';
  } catch (_) {
    return false;
  }
}

// 「可用」= 本地后端（免 Key）或配过 Key 的在线 API。只建了卡、没填 Key 不算可用。
function hasUsableProvider() {
  const profiles = state.bootstrap?.model_profiles || state.bootstrap?.providers || [];
  return profiles.some((profile) => (profile.kind || 'online') === 'local' || profile.has_api_key);
}

function setOnboardingStep(step) {
  const first = $('#onboardingStepPreset');
  const second = $('#onboardingStepKey');
  if (first) first.classList.toggle('is-active', step === 1);
  if (second) second.classList.toggle('is-active', step === 2);
}

export function resetOnboarding() {
  ++onboardingGeneration;
  invalidateOnboardingChecks();
  selectedPresetId = '';
  renderProviderPresetGrid('onboardingPresetGrid');
  $('#onboardingPresetMore').textContent = providerPresetMoreText(ONBOARDING_VISIBLE_PRESETS);
  $('#onboardingKeyRow').hidden = true;
  $('#onboardingRecap').textContent = '';
  fillProviderPresetGuide($('#onboardingGuide'), null, { fallback: '' });
  $('#onboardingUrlField').hidden = true;
  $('#onboardingBaseUrl').value = '';
  $('#onboardingKeyField').hidden = false;
  $('#onboardingApiKey').value = '';
  $('#onboardingModelField').hidden = true;
  setOnboardingModels();
  $('#onboardingModelCustom').value = '';
  $('#onboardingModelStatus').textContent = '';
  $('#onboardingError').textContent = '';
  $('#onboardingBack').hidden = true;
  $('#onboardingTest').hidden = true;
  $('#onboardingSave').hidden = true;
  $('#onboardingSkip').hidden = false;
  setOnboardingStep(1);
}

// 模板仅提供连接信息，模型必须来自实际目录或用户手填。
export function selectOnboardingPreset(presetId) {
  const preset = providerPresetById(presetId);
  if (!preset) return;
  ++onboardingGeneration;
  invalidateOnboardingChecks();
  selectedPresetId = preset.id;
  markProviderPresetCards('onboardingPresetGrid', preset.id);
  const local = preset.kind === 'local';
  const url = String(preset.base_url || '');
  const custom = preset.id === 'custom';
  $('#onboardingKeyRow').hidden = false;
  $('#onboardingRecap').textContent = local
    ? `已选 ${preset.name}（${url || '本机地址'}）：本地服务免 Key，确认地址后检测并选择模型。`
    : `已选 ${preset.name}（${url}）。填好 API Key 后会自动检测模型，可在下方选择。`;
  fillProviderPresetGuide($('#onboardingGuide'), preset, { fallback: '' });
  $('#onboardingUrlField').hidden = !(local || custom);
  $('#onboardingBaseUrl').value = url;
  $('#onboardingKeyField').hidden = !preset.key_required;
  $('#onboardingApiKey').value = '';
  $('#onboardingModelField').hidden = false;
  $('#onboardingModelCustom').value = '';
  setOnboardingModels();
  $('#onboardingModelStatus').textContent = '填好连接信息后自动检测，也可点击「检测模型」或手动填写。';
  $('#onboardingError').textContent = '';
  $('#onboardingBack').hidden = false;
  $('#onboardingTest').hidden = false;
  $('#onboardingSave').hidden = false;
  setOnboardingStep(2);
  // 在线大厂直接落在 Key 输入框；本地/自定义先确认地址。
  if (preset.key_required && !custom) $('#onboardingApiKey').focus();
  else $('#onboardingBaseUrl').focus();
  if (!preset.key_required) scheduleOnboardingModelCheck();
}

function onboardingPayload() {
  const preset = providerPresetById(selectedPresetId);
  if (!preset) return null;
  const kind = preset.kind === 'local' ? 'local' : 'online';
  return {
    preset_id: preset.id,
    name: preset.name,
    kind,
    base_url: $('#onboardingBaseUrl').value.trim(),
    request_format: preset.request_format,
    local_backend: kind === 'local' ? preset.request_format : undefined,
    model: ($('#onboardingModel').value === '__custom__'
      ? $('#onboardingModelCustom').value : $('#onboardingModel').value).trim(),
    api_key: $('#onboardingApiKey').value.trim(),
    reasoning_effort: 'auto',
  };
}

// 保存前的本地校验：把「保存后才知道错」变成当场一句话（服务端仍会再校验一次）。
function validateOnboarding(values, preset, { requireModel = true } = {}) {
  if (!values.base_url) return '请先填写 API URL';
  if (preset.key_required && !values.api_key) return '请先粘贴 API Key';
  if (requireModel && !values.model) return '请先检测并选择模型，或手动填写模型名称';
  return '';
}

function connectionSnapshot(values) {
  const { model, ...connection } = values || {};
  return JSON.stringify(connection);
}

export function scheduleOnboardingModelCheck() {
  invalidateOnboardingChecks();
  // 连接一变，旧目录立即失效；用户明确手填的草稿保留。
  setOnboardingModels([], $('#onboardingModel').value === '__custom__' ? '__custom__' : '');
  $('#onboardingError').textContent = '';
  const preset = providerPresetById(selectedPresetId);
  const values = preset ? onboardingPayload() : null;
  const problem = values ? validateOnboarding(values, preset, { requireModel: false }) : '';
  $('#onboardingModelStatus').textContent = problem || '等待检测模型…';
  if (!values || problem) return;
  modelCheckTimer = setTimeout(() => { loadOnboardingModels(); }, 350);
}

export async function loadOnboardingModels() {
  clearTimeout(modelCheckTimer);
  const preset = providerPresetById(selectedPresetId);
  const values = preset ? onboardingPayload() : null;
  const problem = values ? validateOnboarding(values, preset, { requireModel: false }) : '请先选择一个供应商模板';
  if (problem) {
    $('#onboardingModelStatus').textContent = problem;
    return;
  }
  const generation = ++modelCheckGeneration;
  const snapshot = connectionSnapshot(values);
  const current = () => generation === modelCheckGeneration && $('#onboardingDialog').open
    && snapshot === connectionSnapshot(onboardingPayload());
  const button = $('#onboardingLoadModels');
  button.disabled = true;
  button.textContent = '检测中…';
  $('#onboardingModelStatus').textContent = '正在获取可用模型…';
  try {
    const result = await api('/api/providers/models', { method: 'POST', body: values });
    if (!current()) return;
    const models = Array.isArray(result.models) ? result.models : [];
    const previous = $('#onboardingModel').value;
    setOnboardingModels(models, previous);
    if ($('#onboardingModel').value !== previous) changeOnboardingModel();
    const count = $('#onboardingModel').options.length - 2;
    $('#onboardingModelStatus').textContent = count
      ? `检测到 ${count} 个模型，可切换选择，也可手动填写。`
      : '接口未返回模型，请重试或手动填写模型名称。';
  } catch (error) {
    if (current()) $('#onboardingModelStatus').textContent = `模型检测失败：${error.message}。可重试或手动填写模型名称。`;
  } finally {
    if (current()) {
      button.disabled = false;
      button.textContent = '检测模型';
    }
  }
}

export async function testOnboardingConnection() {
  if ($('#onboardingTest').disabled) return;
  const preset = providerPresetById(selectedPresetId);
  const values = preset ? onboardingPayload() : null;
  const problem = values ? validateOnboarding(values, preset) : '请先选择一个供应商模板';
  if (problem) {
    $('#onboardingError').textContent = problem;
    return;
  }
  const button = $('#onboardingTest');
  const generation = ++connectionTestGeneration;
  const snapshot = JSON.stringify(values);
  const current = () => generation === connectionTestGeneration && $('#onboardingDialog').open
    && snapshot === JSON.stringify(onboardingPayload());
  const label = button.textContent;
  button.disabled = true;
  button.textContent = '测试中…';
  $('#onboardingError').textContent = '正在测试连接…';
  try {
    const result = await api('/api/providers/test', { method: 'POST', body: values });
    if (current()) $('#onboardingError').textContent = providerTestSummary(result);
  } catch (error) {
    if (current()) $('#onboardingError').textContent = `连接失败：${error.message}`;
  } finally {
    if (generation === connectionTestGeneration) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

export async function saveOnboardingProvider() {
  if (saveInFlight) return;
  const preset = providerPresetById(selectedPresetId);
  const values = preset ? onboardingPayload() : null;
  if (!values) {
    $('#onboardingError').textContent = '请先选择一个供应商模板';
    return;
  }
  const problem = validateOnboarding(values, preset);
  if (problem) {
    $('#onboardingError').textContent = problem;
    return;
  }
  const button = $('#onboardingSave');
  const generation = onboardingGeneration;
  saveInFlight = true;
  button.disabled = true;
  button.textContent = '保存中…';
  try {
    const saved = await api('/api/providers', { method: 'POST', body: values });
    syncSavedProvider(saved);
    // 关窗即记「看过引导」（close 事件里统一写），这里只提示结果。
    if (generation === onboardingGeneration) $('#onboardingDialog').close();
    toast(`已添加「${saved.name || preset.name}」，开始聊天吧`);
  } catch (error) {
    if (generation === onboardingGeneration) $('#onboardingError').textContent = error.message;
    else toast(`保存失败：${error.message}`);
  } finally {
    saveInFlight = false;
    button.disabled = false;
    button.textContent = '保存并开始';
  }
}

// 任何关闭路径（跳过按钮 / 右上角 × / Esc）都记一次，之后不再打扰。
export function dismissOnboarding() {
  try {
    localStorage.setItem(ONBOARDING_DISMISS_KEY, '1');
  } catch (_) {
    // 存储不可用（隐私模式）时退化成「本次不再弹」，不影响其它功能。
  }
  resetOnboarding();
}

// 启动时调用：没有可用供应商且没跳过过，才弹向导。
export async function maybeShowOnboarding() {
  if (onboardingDismissed() || hasUsableProvider()) return;
  await loadProviderPresets();
  if (!providerPresets().length) return; // 名单拿不到（离线/旧服务）就不弹空窗，走设置手填
  const dialog = $('#onboardingDialog');
  if (!dialog || dialog.open) return;
  resetOnboarding();
  dialog.showModal();
}
