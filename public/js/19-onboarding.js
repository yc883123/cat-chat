// ============================================================
// 19-onboarding.js —— 首启引导向导：还没有可用供应商时，选模板 → 粘 Key → 开聊
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
  $('#onboardingModel').value = '';
  $('#onboardingError').textContent = '';
  $('#onboardingBack').hidden = true;
  $('#onboardingTest').hidden = true;
  $('#onboardingSave').hidden = true;
  $('#onboardingSkip').hidden = false;
  setOnboardingStep(1);
}

// 选模板 → 进第 2 步：本地后端要地址不要 Key，在线大厂只要 Key，自定义要地址+Key+模型名。
export function selectOnboardingPreset(presetId) {
  const preset = providerPresetById(presetId);
  if (!preset) return;
  selectedPresetId = preset.id;
  markProviderPresetCards('onboardingPresetGrid', preset.id);
  const local = preset.kind === 'local';
  const url = String(preset.base_url || '');
  const custom = preset.id === 'custom';
  $('#onboardingKeyRow').hidden = false;
  $('#onboardingRecap').textContent = local
    ? `已选 ${preset.name}（${url || '本机地址'}）：本地服务免 Key，确认地址与模型名后保存即可。`
    : `已选 ${preset.name}（${url}）${preset.model ? `，默认使用模型 ${preset.model}` : ''}。`;
  fillProviderPresetGuide($('#onboardingGuide'), preset, { fallback: '' });
  $('#onboardingUrlField').hidden = !(local || custom);
  $('#onboardingBaseUrl').value = url;
  $('#onboardingKeyField').hidden = !preset.key_required;
  $('#onboardingApiKey').value = '';
  // 模板自带推荐模型就不用让用户再选；自定义/本地没有推荐值，必须自己填（后端校验非空）。
  $('#onboardingModelField').hidden = Boolean(preset.model);
  $('#onboardingModel').value = preset.model || '';
  $('#onboardingError').textContent = '';
  $('#onboardingBack').hidden = false;
  $('#onboardingTest').hidden = false;
  $('#onboardingSave').hidden = false;
  setOnboardingStep(2);
  // 在线大厂直接落在 Key 输入框；本地/自定义先确认地址。
  if (preset.key_required && !custom) $('#onboardingApiKey').focus();
  else $('#onboardingBaseUrl').focus();
}

function onboardingPayload() {
  const preset = providerPresetById(selectedPresetId);
  if (!preset) return null;
  const kind = preset.kind === 'local' ? 'local' : 'online';
  return {
    preset_id: preset.id,
    name: preset.name,
    kind,
    base_url: ($('#onboardingBaseUrl').value || preset.base_url || '').trim(),
    request_format: preset.request_format,
    local_backend: kind === 'local' ? preset.request_format : undefined,
    model: ($('#onboardingModel').value || preset.model || '').trim(),
    api_key: $('#onboardingApiKey').value.trim(),
    reasoning_effort: 'auto',
  };
}

// 保存前的本地校验：把「保存后才知道错」变成当场一句话（服务端仍会再校验一次）。
function validateOnboarding(values, preset) {
  if (!values.base_url) return '请先填写 API URL';
  if (preset.key_required && !values.api_key) return '请先粘贴 API Key';
  if (!values.model) return '请先填写模型名称（本地服务可先填一个已下载的模型，保存后在设置里点「检查模型」）';
  return '';
}

export async function testOnboardingConnection() {
  const preset = providerPresetById(selectedPresetId);
  const values = preset ? onboardingPayload() : null;
  const problem = values ? validateOnboarding(values, preset) : '请先选择一个供应商模板';
  if (problem) {
    $('#onboardingError').textContent = problem;
    return;
  }
  const button = $('#onboardingTest');
  const label = button.textContent;
  button.disabled = true;
  button.textContent = '测试中…';
  $('#onboardingError').textContent = '正在测试连接…';
  try {
    const result = await api('/api/providers/test', { method: 'POST', body: values });
    $('#onboardingError').textContent = providerTestSummary(result);
  } catch (error) {
    $('#onboardingError').textContent = `连接失败：${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

export async function saveOnboardingProvider() {
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
  button.disabled = true;
  button.textContent = '保存中…';
  try {
    const saved = await api('/api/providers', { method: 'POST', body: values });
    syncSavedProvider(saved);
    // 关窗即记「看过引导」（close 事件里统一写），这里只提示结果。
    $('#onboardingDialog').close();
    toast(`已添加「${saved.name || preset.name}」，开始聊天吧`);
  } catch (error) {
    $('#onboardingError').textContent = error.message;
  } finally {
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
