// ============================================================
// 09-settings.js —— 拆分自 public/app.js 第 3115-4672 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, $$, api, applyAppearance, escapeHtml, state, toast } from "./01-core.js";
import { applyConversationAgent, populateComposerModels, populateModels, renderAgents, updateUnloadModelButton } from "./07-models-agents.js";
import { closeAgentPromptPresetPanel, currentAgentFixedSkillIds, renderAgentPromptPresetList } from "./08-conversations.js";
import { skillList } from "./13-skill-refs.js";
import { switchSettingsTab } from "./15-bind-events.js";
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
  const theme = appearance.theme || 'system';
  const skin = appearance.skin || 'violet';
  $$('input[name="appearanceTheme"]').forEach((input) => { input.checked = input.value === theme; });
  $$('input[name="appearanceSkin"]').forEach((input) => { input.checked = input.value === skin; });
  applyAppearance({ theme, skin });
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

function providerCardMarkup(provider) {
  const id = escapeHtml(provider.id || '');
  const name = escapeHtml(provider.name || '未命名供应商');
  const format = PROVIDER_FORMAT_LABELS[provider.request_format] || escapeHtml(provider.request_format || '未指定格式');
  // 卡片压成两行：名称 + 脚行（格式标签 + 当前角标）。模型名不再上卡片（在弹层里看），
  // 卡片更矮、一行能放下的信息更整齐。
  return `
    <div class="provider-card${provider.is_default ? ' is-default' : ''}" data-provider-card="${id}" role="button" tabindex="0" aria-label="编辑 ${name}">
      <button class="provider-card-delete" type="button" data-provider-delete="${id}" title="删除 ${name}" aria-label="删除 ${name}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>
      </button>
      <span class="provider-card-name" title="${name}">${name}</span>
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
  $('#providerDialogTitle').textContent = isNew ? '添加 API 供应商' : (provider.name || 'API 供应商');
  $('#providerDialogSubtitle').textContent = inferredKind === 'local' ? '本地 API' : '在线 API';
  // 卡片点开即可编辑（不再有「只读 → 点编辑」两态）。
  setProviderEditMode(true);
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
    hint.textContent = '已强制设为支持图片；Naiba-chat 会把用户图片直接交给该模型。';
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

export async function saveProvider(event) {
  event.preventDefault();
  try {
    const values = providerFormValue();
    if (values.model && (!values.context_window || !values.max_output_tokens)) {
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
    ['providers', 'model_profiles'].forEach((key) => {
      const list = state.bootstrap[key] || (state.bootstrap[key] = []);
      const index = list.findIndex((item) => item.id === saved.id);
      if (index >= 0) list[index] = saved;
      else list.push(saved);
    });
    $('#providerId').value = saved.id;
    populateModels();
    const visionSelect = $('#visionProvider');
    if (visionSelect) delete visionSelect.dataset.populated;
    populateVisionSettings();
    toast('API 供应商已保存');
    cancelProviderEdit();
  } catch (error) {
    $('#providerError').textContent = error.message;
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

export async function testProvider() {
  $('#providerError').textContent = '正在测试连接…';
  try {
    const result = await api('/api/providers/test', { method: 'POST', body: providerFormValue() });
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
    $('#providerError').textContent = `推理连接成功：${result.response}；视觉：${vision}；来源：${source}${proxyNote}`;
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
  renderImageCompressRow();
  if ($('#imageCacheSize')) $('#imageCacheSize').textContent = formatBytes(Number(state.bootstrap.image_cache_bytes || 0));
  renderProxySettings();
  renderWorkspaceControl();
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

export async function refreshImageCacheSize() {
  try {
    const result = await api('/api/imaging/stats');
    state.bootstrap.image_cache_bytes = Number(result.image_cache_bytes || 0);
    if ($('#imageCacheSize')) $('#imageCacheSize').textContent = formatBytes(state.bootstrap.image_cache_bytes);
  } catch (_) { /* 打开设置页时统计失败不打扰用户 */ }
}

export async function cleanImageCache() {
  const btn = $('#cleanImageCache');
  if (!btn) return;
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = '清理中…';
  try {
    const result = await api('/api/imaging/clean', { method: 'POST', body: {} });
    state.bootstrap.image_cache_bytes = Number(result.size || 0);
    $('#imageCacheSize').textContent = formatBytes(Number(result.size || 0));
    toast(`已清理 ${formatBytes(Number(result.freed || 0))}（删除 ${Number(result.removed || 0)} 个文件，按时间从旧到新）`);
  } catch (error) {
    toast(`清理失败：${error.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
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
  state.bootstrap.default_agent_id = data.default_agent_id || 'general';
}

// 卡片上的提示词摘要长度（超出截断，完整内容在弹层里看）。
const AGENT_PROMPT_PREVIEW_LIMIT = 140;

// 弹层里新选的头像文件（保存时才上传；新建 Agent 此时还没有 id，必须延后到保存后）。
let agentAvatarFile = null;

export function agentAvatarUrl(agent) {
  const file = String(agent?.avatar || '');
  return file ? `/api/agents/avatar/${encodeURIComponent(file)}` : '';
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
  // 卡片上不再标「默认」角标、也不给默认 Agent 加高亮：默认项由顶栏 Agent 选择器体现，
  // 卡片上一旦有强调色/角标，会被误读成"当前选中/正在编辑的那一个"。
  const badges = [
    agent.built_in ? '<span class="agent-card-tag">内置</span>' : '',
  ].join('');
  const toolSetName = toolScopeLabel(agent.tool_scope);
  return `
    <div class="agent-card" data-agent-card="${id}" role="button" tabindex="0" aria-label="编辑 ${name}">
      ${agent.built_in ? '' : `<button class="agent-card-delete" type="button" data-agent-delete="${id}" title="删除 ${name}" aria-label="删除 ${name}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>`}
      <span class="agent-card-name" title="${name}">${avatar ? `<img class="agent-card-avatar" src="${escapeHtml(avatar)}" alt="">` : ''}${name}</span>
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
  toast('头像已选择，点「保存 Agent」后生效');
}

// Agent 设置：工具选择的联动规则。创建者工具依赖其查询工具（与后端
// JOB_CREATOR_TOOL_DEPS / 依赖闭包保持一致）。选中创建者时自动带上查询工具；
// 取消某个查询工具时，若仍有选中的创建者依赖它，则同步取消该创建者，保证
// “创建者被允许 ⇔ 其描述里让你查询的工具也被允许”的 invariant 不被打破。
export const AGENT_TOOL_DEP_RULES = {
  run_in_background: ['job_output', 'job_status', 'job_wait', 'job_kill'],
  subagent: ['job_output'],
  comfyui_batch: ['job_output', 'job_status', 'job_wait'],
};

export function applyAgentToolDependency(scope, changedTool, checked) {
  const result = new Set(scope);
  if (checked) {
    result.add(changedTool);
    const deps = AGENT_TOOL_DEP_RULES[changedTool];
    if (deps) deps.forEach((dep) => result.add(dep));
  } else {
    result.delete(changedTool);
    // 取消的若是某创建者必需的查询工具，则把这些创建者也一并取消。
    for (const [creator, deps] of Object.entries(AGENT_TOOL_DEP_RULES)) {
      if (deps.includes(changedTool) && result.has(creator)) result.delete(creator);
    }
  }
  return [...result];
}

// 把工具集补齐依赖闭包：选中创建者工具时自动带上它依赖的查询工具。
// 与后端依赖闭包保持一致，保证这里勾选的状态就是运行时会放行的 allowed_tools。
export function normalizeToolScope(scope) {
  const result = new Set(scope || []);
  for (const [creator, deps] of Object.entries(AGENT_TOOL_DEP_RULES)) {
    if (result.has(creator)) deps.forEach((dep) => result.add(dep));
  }
  return [...result];
}

// —— 工具集：卡片态（内置预设 + 我的工具集）↔ 编辑态 ——
// 两态同框叠放、弹层高度固定：点任意卡片（含「添加」卡）都先按一下再向上滑出，工具列表从下方滑入。
const TOOL_SWAP_MS = 130;  // = CSS 里 .tool-preset-view.is-leaving 的 50ms 延迟 + 80ms 过渡
// 「添加自定义工具集」的起点：不跟随当前选中项，固定以标准模式为底稿。
const DEFAULT_TOOL_SET_PRESET_ID = 'standard';
const TOOL_SET_ADD_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg>';
const TOOL_SET_DEL_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>';

// 当前工具集命中哪张卡片：内置预设优先，其次「我的工具集」；都不匹配返回 null。
export function matchToolScope(scope) {
  const current = new Set(scope || []);
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
// 老配置里「当前未注册」的工具（退役工具、掉线的 MCP 工具）不参与匹配与计数：
// 它们已经不存在了，算进去只会让同一套工具集因为几个幽灵名字匹配不上。
export function toolScopeLabel(scope) {
  const raw = Array.isArray(scope) ? scope.filter(Boolean) : [];
  if (!raw.length) return '未限制（全部工具）';
  const known = knownToolNames();
  const tools = known.size ? raw.filter((name) => known.has(name)) : raw;
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
    const count = usableTemplateTools(template).length;
    const name = escapeHtml(template.name);
    return `<div class="tool-preset-card${active ? ' is-active' : ''}" role="button" tabindex="0"
      data-tool-template-card="${escapeHtml(template.id)}" title="套用「${name}」">
      <button type="button" class="tool-preset-card-del" data-tool-template-del="${escapeHtml(template.id)}"
              title="删除工具集「${name}」" aria-label="删除工具集「${name}」">${TOOL_SET_DEL_SVG}</button>
      <b>${name}</b>
      <small>我的工具集</small>
      <em>${count} 个工具</em>
    </div>`;
  });
  box.innerHTML = [...presetCards, ...templateCards].join('') + `
    <div class="tool-preset-card tool-preset-card-add" role="button" tabindex="0"
         data-tool-preset-add title="从当前勾选开始，添加一套自定义工具集">
      ${TOOL_SET_ADD_SVG}
      <span>添加自定义工具集</span>
    </div>`;
  const summary = $('#agentToolPresetState');
  if (summary) summary.textContent = toolSetSummary();
  updateToolCounter();
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
//   「添加」卡 → 以「标准模式」预设为起点（不跟随当前选中项）、命名栏留空（留空自动命名）。
export function openAgentToolEditor({ presetId = '', templateId = '' } = {}) {
  const presets = state.toolCatalog?.presets || [];
  const preset = presetId ? presets.find((item) => item.id === presetId) : null;
  const template = templateId
    ? loadToolTemplates().find((item) => item.id === templateId) : null;
  // 底稿：点了预设卡就是该预设；点了「添加」卡则固定用标准模式（不跟随当前选中项）。
  const base = preset || (presetId || templateId ? null
    : presets.find((item) => item.id === DEFAULT_TOOL_SET_PRESET_ID) || null);
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
  }
  const nameInput = $('#agentToolSetName');
  // 命名栏只预填「被点的那张卡」的名字；「添加」卡没有名字，留空（保存时自动命名）。
  if (nameInput) nameInput.value = preset ? preset.name : (template ? template.name : '');
  state.agentToolFilter = '';
  const filter = $('#agentToolFilter');
  if (filter) filter.value = '';
  renderToolScopeList();
  syncAgentToolCheckboxes($('#agentToolScope'));
  swapToolView(true);
  // 上面 syncAgentToolCheckboxes → updateToolPresetUI 会重绘卡片（编辑态还没显示），
  // 所以「按下」状态要在重绘之后再按 key 找回卡片打上。
  markPickedToolCard({ presetId, templateId });
  nameInput?.focus();
  nameInput?.select();
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

export function closeAgentToolEditor() {
  state.agentToolEditingId = '';
  swapToolView(false);
  renderAgentToolPresetCards();
}

// 保存工具集：写入后端「我的工具集」（编辑中的原地更新），当前勾选已经是表单里的值，无需再套用。
export async function saveAgentToolSet() {
  const known = knownToolNames();
  const tools = state.agentFormToolScope.filter((name) => known.has(name));
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

// 模板里可能存过已被移除的工具名：复刻时只应用当前目录里还存在的。
export function usableTemplateTools(template) {
  const known = knownToolNames();
  return (template.tools || []).filter((name) => known.has(name));
}

export async function deleteToolTemplate(templateId) {
  const template = loadToolTemplates().find((item) => item.id === templateId);
  if (!template) return;
  if (!confirm(`确定删除工具集「${template.name}」吗？`)) return;
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
  // 每次打开表单都回到卡片态（编辑中的工具集不跨表单残留）。
  state.agentToolEditingId = '';
  swapToolView(false);
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
  });
  const span = document.createElement('span');
  const b = document.createElement('b');
  b.textContent = tool.name;
  if (AGENT_TOOL_DEP_RULES[tool.name]) {
    b.title = '选中后会自动带上其依赖的查询工具（job_output/job_status/job_wait/job_kill 等）。';
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
      scope = applyAgentToolDependency(scope, tool.name, all.checked);
    }
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

function buildGroupBlock(group, toolMap, list) {
  const groupEl = document.createElement('div');
  groupEl.className = 'agent-tool-group collapsed';
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
  allCb.title = `全选/取消全选「${group.name}」分类下的所有工具`;
  allCb.addEventListener('click', (e) => e.stopPropagation());
  allCb.addEventListener('change', () => {
    let scope = state.agentFormToolScope;
    for (const name of group.tools || []) {
      const tool = toolMap.get(name);
      if (tool) scope = applyAgentToolDependency(scope, tool.name, allCb.checked);
    }
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
  body.hidden = true;
  const direct = (group.direct_tools || group.tools || [])
    .map((name) => toolMap.get(name)).filter(Boolean);
  if (direct.length) body.append(buildToolGrid(direct, list));
  for (const sub of group.subgroups || []) {
    const tools = (sub.tools || []).map((name) => toolMap.get(name)).filter(Boolean);
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
  if (filter) renderFilteredScope(list, catalog, filter);
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
  const payload = {
    command_timeout: Number($('#commandTimeout')?.value || 120),
    context_warning_percent: warningRaw === '' ? 80 : Number(warningRaw),
    local_first_byte_timeout_seconds: firstByteRaw === '' ? 120 : Number(firstByteRaw),
    agent_step_limit: maxStepsRaw === '' ? 200 : Number(maxStepsRaw),
    context_reset_seed_template: String($('#contextResetSeedTemplate')?.value || ''),
    workspace_dir: $('#workspaceDir')?.value.trim() || '',
    imaging: {
      image_upload_original: Boolean($('#imageUploadOriginal')?.checked),
      image_max_pixels: Number($('#imageMaxPixels')?.value || 2000000),
      thumbnail_max_pixels: Number($('#thumbnailMaxPixels')?.value || 500000),
      auto_clean_limit_mb: Number($('#autoCleanLimitMb')?.value ?? 256),
    },
    proxy,
  };
  const result = await api('/api/settings', { method: 'POST', body: payload });
  Object.assign(state.bootstrap.settings, result.settings);
  state.bootstrap.resolved_workspace_dir = result.resolved_workspace_dir || state.bootstrap.resolved_workspace_dir;
  if ($('#resolvedWorkspaceDir')) $('#resolvedWorkspaceDir').textContent = state.bootstrap.resolved_workspace_dir || '-';
  if (result.image_cache_bytes !== undefined) {
    state.bootstrap.image_cache_bytes = result.image_cache_bytes;
    if ($('#imageCacheSize')) $('#imageCacheSize').textContent = formatBytes(Number(result.image_cache_bytes || 0));
  }
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

