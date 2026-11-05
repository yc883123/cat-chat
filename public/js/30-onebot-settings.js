// QQ 渠道设置只消费脱敏配置；口令仅在保存请求中提交，不写前端状态或本地存储。
import { $, api, copyText, escapeHtml, state } from './01-core.js';

let currentConfig = {};
let bindings = [];
let conversations = [];
let pollTimer = 0;
let active = false;
let saving = false;
let bindingBusy = false;
let bound = false;
let generation = 0;
let authorized = false;
let configLoaded = false;
let conversationsLoaded = false;
let statusFailed = false;

function currentRequest(requestGeneration) {
  return active && requestGeneration === generation;
}

function feedback(message = '', failed = false) {
  $('#onebotFeedback').textContent = message;
  $('#onebotFeedback').classList.toggle('onebot-error', failed);
}

function updateAddress() {
  const host = String(currentConfig.listen_host || '127.0.0.1');
  const localHost = ['0.0.0.0', '::', '[::]'].includes(host) ? '127.0.0.1' : host;
  const addressHost = localHost.includes(':') && !localHost.startsWith('[') ? `[${localHost}]` : localHost;
  $('#onebotAddress').value = `ws://${addressHost}:${currentConfig.listen_port || 8766}${currentConfig.path || '/onebot/v11/ws'}`;
  $('#onebotCopyAddress').disabled = false;
}

function renderStatus(status = {}) {
  $('#onebotListening').textContent = status.listening ? '已监听' : (status.enabled ? '未监听' : '未启用');
  $('#onebotConnected').textContent = status.connected ? '已连接' : '未连接';
  $('#onebotAccount').textContent = status.account_id || '—';
  const error = $('#onebotLastError');
  error.hidden = !status.error;
  error.textContent = status.error ? `最近错误：${status.error}` : '';
  if (status.account_id && !$('#onebotBotId').value) $('#onebotBotId').value = status.account_id;
}

function fillSelect(select, options, value, fallbackLabel) {
  select.replaceChildren(new Option(fallbackLabel, ''));
  for (const item of options) select.add(new Option(item.label, item.value));
  if (value && !options.some((item) => item.value === value)) select.add(new Option(`当前配置（不可用）：${value}`, value));
  select.value = value || '';
}

function populateConfig() {
  const cfg = currentConfig;
  $('#onebotEnabled').checked = Boolean(cfg.enabled);
  $('#onebotListenHost').value = cfg.listen_host || '127.0.0.1';
  $('#onebotListenPort').value = cfg.listen_port || 8766;
  $('#onebotPath').value = cfg.path || '/onebot/v11/ws';
  $('#onebotAccessToken').value = '';
  $('#onebotTokenStatus').textContent = cfg.has_access_token ? '已配置' : '未配置';
  $('#onebotAllowedUsers').value = (cfg.allowed_user_ids || []).join('\n');
  $('#onebotAllowGroups').checked = Boolean(cfg.allow_groups);
  $('#onebotAllowedGroups').value = (cfg.allowed_group_ids || []).join('\n');
  $('#onebotGroupsField').hidden = !cfg.allow_groups;
  const profiles = state.bootstrap?.model_profiles || [];
  fillSelect($('#onebotDefaultModel'), profiles.map((item) => ({
    value: `${item.model_key}\n${item.model || ''}`, label: `${item.name || item.model || item.model_key} · ${item.model || ''}`,
  })), cfg.default_model_key ? `${cfg.default_model_key}\n${cfg.default_model_name || ''}` : '', '沿用全局默认模型');
  fillSelect($('#onebotDefaultAgent'), (state.bootstrap?.agents || []).map((item) => ({ value: item.id, label: item.name || item.id })), cfg.default_agent_id || '', '沿用全局默认 Agent');
}

function renderBindings() {
  const list = $('#onebotBindings');
  const labels = new Map(conversations.map((item) => [String(item.id), item.title || '未命名会话']));
  list.innerHTML = bindings.length ? bindings.map((item) => `
    <div class="onebot-binding"><div><strong>QQ ${escapeHtml(item.qq_user_id)}</strong><span>${item.scope === 'group' ? `群 ${escapeHtml(item.qq_group_id)} · ` : '私聊 · '}机器人 ${escapeHtml(item.channel_account_id)}</span><span>→ ${escapeHtml(labels.get(String(item.conversation_id)) || item.conversation_id)}</span></div><button class="control-button" type="button" data-onebot-unbind="${escapeHtml(bindingIdentity(item))}" ${bindingBusy ? 'disabled' : ''}>解绑</button></div>
  `).join('') : '<p class="hint">还没有绑定。保存 QQ 白名单后，在下方添加。</p>';
}

function bindingIdentity(item) {
  return JSON.stringify([item.channel_account_id, item.scope, item.qq_user_id, item.qq_group_id || '']);
}

function renderConversationOptions() {
  const select = $('#onebotConversationId');
  const selected = select.value;
  fillSelect(select, conversations.map((item) => ({ value: String(item.id), label: item.title || '未命名会话' })), selected, conversations.length ? '选择会话' : '暂无会话，可选择新建专用会话');
}

function updateBindMode() {
  const createNew = $('#onebotBindMode').value === 'new';
  const isGroup = $('#onebotBindScope').value === 'group';
  $('#onebotConversationField').hidden = createNew;
  $('#onebotConversationId').required = !createNew;
  $('#onebotTitleField').hidden = !createNew;
  $('#onebotBindGroupField').hidden = !isGroup;
  $('#onebotGroupId').required = isGroup;
}

function setAccessible(accessible = authorized) {
  authorized = accessible;
  $('#onebotConfigFields').disabled = !accessible || saving;
  $('#onebotBindFields').disabled = !accessible || bindingBusy;
}

function visible() {
  return active && $('#settingsDialog')?.open && !$('[data-settings-panel="onebot"]')?.hidden;
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (visible()) pollTimer = setTimeout(() => void refreshStatus(), 5000);
}

async function refreshStatus({ populate = false, loadConversations = false } = {}) {
  const requestGeneration = generation;
  try {
    const result = await api('/api/onebot/status');
    if (!currentRequest(requestGeneration)) return;
    currentConfig = result.config || {};
    bindings = result.bindings || [];
    renderStatus(result.status);
    updateAddress();
    if (populate || !configLoaded) {
      populateConfig();
      configLoaded = true;
    }
    setAccessible(true);
    // 身份数组与按钮必须同步更新，不能等另一次网络请求回来后才重画。
    renderBindings();
    if (statusFailed && !saving && !bindingBusy) feedback();
    statusFailed = false;
    if (loadConversations || !conversationsLoaded) {
      const data = await api('/api/conversations');
      if (!currentRequest(requestGeneration)) return;
      conversations = data.conversations || [];
      conversationsLoaded = true;
      renderConversationOptions();
    }
    renderBindings();
  } catch (error) {
    if (!currentRequest(requestGeneration)) return;
    statusFailed = true;
    feedback(`读取 QQ 渠道失败：${error.message}`, true);
    if (error.status === 403) setAccessible(false);
  } finally {
    if (requestGeneration === generation) schedulePoll();
  }
}

function parseIds(value, label) {
  const items = [...new Set(String(value || '').split(/[\s,，;；]+/).filter(Boolean))];
  if (items.some((item) => !/^\d+$/.test(item))) throw new Error(`${label}只能填写数字，多个号码用逗号、空格或换行分隔`);
  if (items.length > 200) throw new Error(`${label}最多填写 200 个号码`);
  return items;
}

async function saveConfig(event) {
  event.preventDefault();
  if (saving || !active || !authorized) return;
  const requestGeneration = generation;
  try {
    const model = $('#onebotDefaultModel').value.split('\n');
    const config = {
      enabled: $('#onebotEnabled').checked,
      listen_host: $('#onebotListenHost').value.trim(),
      listen_port: Number($('#onebotListenPort').value),
      path: $('#onebotPath').value.trim(),
      allowed_user_ids: parseIds($('#onebotAllowedUsers').value, 'QQ 白名单'),
      allow_groups: $('#onebotAllowGroups').checked,
      allowed_group_ids: parseIds($('#onebotAllowedGroups').value, '群白名单'),
      default_model_key: model[0] || '', default_model_name: model[1] || '',
      default_agent_id: $('#onebotDefaultAgent').value,
    };
    const token = $('#onebotAccessToken').value.trim();
    if (token) config.access_token = token;
    saving = true;
    setAccessible();
    feedback('正在保存 QQ 渠道设置…');
    const result = await api('/api/settings', { method: 'POST', body: { onebot: config } });
    if (!currentRequest(requestGeneration)) return;
    currentConfig = result.settings?.onebot || currentConfig;
    if (state.bootstrap?.settings) state.bootstrap.settings.onebot = currentConfig;
    populateConfig();
    updateAddress();
    renderStatus(result.settings?.onebot_status);
    feedback('QQ 渠道设置已保存');
    await refreshStatus();
  } catch (error) {
    if (currentRequest(requestGeneration)) feedback(`保存失败：${error.message}`, true);
  } finally {
    saving = false;
    if (currentRequest(requestGeneration)) setAccessible();
    else if (visible()) void refreshStatus();
  }
}

async function bindConversation(event) {
  event.preventDefault();
  if (bindingBusy || !active || !authorized) return;
  const requestGeneration = generation;
  bindingBusy = true;
  setAccessible();
  try {
    const isGroup = $('#onebotBindScope').value === 'group';
    await api('/api/onebot/bind', { method: 'POST', body: {
      mode: $('#onebotBindMode').value,
      account_id: $('#onebotBotId').value.trim(), qq_user_id: $('#onebotUserId').value.trim(),
      scope: isGroup ? 'group' : 'private', qq_group_id: isGroup ? $('#onebotGroupId').value.trim() : '',
      conversation_id: $('#onebotConversationId').value, title: $('#onebotNewTitle').value.trim(),
    } });
    if (!currentRequest(requestGeneration)) return;
    feedback('QQ 会话绑定已保存');
    await refreshStatus({ loadConversations: true });
  } catch (error) {
    if (currentRequest(requestGeneration)) feedback(`绑定失败：${error.message}`, true);
  } finally {
    bindingBusy = false;
    if (currentRequest(requestGeneration)) { setAccessible(); renderBindings(); }
    else if (visible()) void refreshStatus();
  }
}

async function unbindConversation(identity) {
  if (bindingBusy || !active || !authorized) return;
  const item = bindings.find((entry) => bindingIdentity(entry) === identity);
  if (!item) { feedback('该绑定已发生变化，请刷新后再操作', true); return; }
  const requestGeneration = generation;
  bindingBusy = true;
  renderBindings();
  setAccessible();
  try {
    await api('/api/onebot/unbind', { method: 'POST', body: {
      account_id: item.channel_account_id, scope: item.scope, qq_user_id: item.qq_user_id, qq_group_id: item.qq_group_id,
    } });
    if (!currentRequest(requestGeneration)) return;
    feedback('已解除绑定，会话和消息仍保留');
    await refreshStatus();
  } catch (error) {
    if (currentRequest(requestGeneration)) feedback(`解绑失败：${error.message}`, true);
  } finally {
    bindingBusy = false;
    if (currentRequest(requestGeneration)) { setAccessible(); renderBindings(); }
    else if (visible()) void refreshStatus();
  }
}

export async function openOnebotSettings() {
  active = true;
  generation += 1;
  configLoaded = false;
  conversationsLoaded = false;
  statusFailed = false;
  setAccessible(false);
  feedback();
  await refreshStatus({ populate: true, loadConversations: true });
}

export function closeOnebotSettings() {
  active = false;
  generation += 1;
  clearTimeout(pollTimer);
  if ($('#onebotAccessToken')) $('#onebotAccessToken').value = '';
}

export function bindOnebotSettingsEvents() {
  if (bound || !$('#onebotConfigForm')) return;
  bound = true;
  $('#onebotConfigForm').addEventListener('submit', (event) => void saveConfig(event));
  $('#onebotBindForm').addEventListener('submit', (event) => void bindConversation(event));
  $('#onebotRefresh').addEventListener('click', () => { feedback(); void refreshStatus({ loadConversations: true }); });
  $('#onebotAllowGroups').addEventListener('change', () => { $('#onebotGroupsField').hidden = !$('#onebotAllowGroups').checked; });
  $('#onebotBindMode').addEventListener('change', updateBindMode);
  $('#onebotBindScope').addEventListener('change', updateBindMode);
  $('#onebotBindings').addEventListener('click', (event) => {
    const button = event.target.closest('[data-onebot-unbind]');
    if (button) void unbindConversation(button.dataset.onebotUnbind);
  });
  $('#onebotCopyAddress').addEventListener('click', async () => {
    try { await copyText($('#onebotAddress').value); feedback('已复制 NapCat 连接地址'); }
    catch (error) { feedback(`复制失败：${error.message}`, true); }
  });
  $('#settingsDialog').addEventListener('close', closeOnebotSettings);
  $('#openSettings')?.addEventListener('click', () => {
    if (!$('[data-settings-panel="onebot"]')?.hidden) void openOnebotSettings();
  });
  updateBindMode();
}
