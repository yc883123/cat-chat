// ============================================================
// 05-bootstrap.js —— 拆分自 public/app.js 第 1502-1605 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, $$, api, initializeAppearance, setServerStatus, state, syncAppearanceFromBootstrap, toast } from "./01-core.js";
import { loadTasks, startTaskSync } from "./06-tasks-plans.js";
import { populateModels, renderAgents, renderUpdateStatus } from "./07-models-agents.js";
import { loadConversationPromptPresets, loadConversations, restoreSidebarWidth, setSidebarScrollToActive, startConversationSync } from "./08-conversations.js";
import { loadProviderPresets, migrateLegacyToolTemplates, populateAppearanceSettings, populateRuntimeSettings, populateSearchSettings, populateVisionSettings, renderAgentManager, renderMcp, renderProviders, renderSkills, startMcpPoll } from "./09-settings.js";
import { loadStarterPrompts } from "./12-chat-input.js";
import { maybeShowOnboarding } from "./19-onboarding.js";
export async function authenticate(token) {
  const response = await fetch('/api/auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  if (!response.ok) throw new Error('访问口令不正确');
  state.token = token;
  localStorage.setItem('naibaChatToken', token);
}

export function renderNetworkAccess() {
  const access = state.bootstrap || {};
  const configuredHost = String(access.settings?.host || '0.0.0.0');
  const pendingRestart = Boolean(access.lan_restart_required);
  const address = access.lan_enabled && access.lan_url
    ? access.lan_url
    : (pendingRestart || configuredHost === '127.0.0.1' ? '当前仅本机访问' : '手机访问不可用');
  const reason = pendingRestart
    ? '手机访问已启用，请完全退出并重新启动 Cat Chat。'
    : (access.lan_reason || '手机与电脑需连接同一局域网。');
  $('#lanAddress').textContent = address;
  $('#connectionAddress').textContent = access.lan_url || access.local_url || '未检测到可用地址';
  $('#connectionReason').textContent = reason;
  $('#enableLanActions').hidden = access.lan_enabled || pendingRestart || configuredHost === '0.0.0.0';
  const copyButton = $('#copyAddress');
  copyButton.disabled = !access.lan_enabled || !access.lan_url;
  copyButton.title = copyButton.disabled ? reason : '复制手机访问地址';
}

export async function enableLanAccess() {
  try {
    const result = await api('/api/settings', { method: 'POST', body: { host: '0.0.0.0' } });
    Object.assign(state.bootstrap.settings, result.settings || {});
    state.bootstrap.lan_restart_required = Boolean(result.restart_required);
    renderNetworkAccess();
    toast('手机访问已启用，请完全退出并重新启动 Cat Chat');
  } catch (error) {
    toast(`启用手机访问失败：${error.message}`);
  }
}

export async function initialize() {
  // 尽早应用本地偏好，避免首屏闪烁；bootstrap 返回后再以服务端配置同步。
  initializeAppearance();
  restoreSidebarWidth();
  setSidebarScrollToActive(true);
  try {
    state.bootstrap = await api('/api/bootstrap');
  } catch (error) {
    state.token = '';
    localStorage.removeItem('naibaChatToken');
    localStorage.removeItem('lanSkillToken');
    $('#authDialog').showModal();
    return;
  }
  syncAppearanceFromBootstrap(state.bootstrap);
  populateAppearanceSettings();
  const migration = state.bootstrap.data_location?.migration;
  if (migration?.migrated) {
    const restored = [
      migration.config ? 'API 配置' : '',
      migration.data ? '对话数据' : '',
    ].filter(Boolean).join('和');
    toast(`已从旧目录恢复${restored || '数据'}`);
  }
  setServerStatus(true);
  state.workspaces = Array.isArray(state.bootstrap?.workspaces) ? state.bootstrap.workspaces : [];
  // 「我的工具集」随 bootstrap 带回（后端 config.json 的 tool_sets），卡片渲染不再依赖额外请求。
  state.toolTemplates = Array.isArray(state.bootstrap?.tool_sets) ? state.bootstrap.tool_sets : [];
  renderNetworkAccess();
  populateModels();
  renderAgents();
  renderAgentManager();
  // 老版本把工具集存在 localStorage（冻结版每次退出都会清空）：这里做一次性搬迁，不阻塞启动。
  void migrateLegacyToolTemplates();
  // 恢复模式 Tab 状态
  $$('.mode-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.mode === state.mode));
  populateRuntimeSettings();
  populateVisionSettings();
  populateSearchSettings();
  renderSkills();
  renderProviders();
  renderMcp();
  renderUpdateStatus(state.bootstrap.update || {});
  await loadConversationPromptPresets();
  await loadConversations();
  await loadStarterPrompts();
  await loadTasks();
  startTaskSync();
  startConversationSync();
  startMcpPoll();
  // 供应商模板（「只填 API Key」的接入名单）与首启引导：设置弹层与向导共用同一份名单，
  // 拿不到名单时退化成全手填路径，不弹空窗、不阻塞启动。
  await loadProviderPresets();
  await maybeShowOnboarding();
  startUpdatePoll();
}

// 后台检查可能启动时才开始（checking 阶段），前端以 30 秒间隔轮询感知结果。
export function startUpdatePoll() {
  if (state.updatePollTimer) return;
  state.updatePollTimer = window.setInterval(async () => {
    const status = state.bootstrap.update || {};
    if (['checking', 'downloading', 'restarting'].includes(status.phase)) return;
    try {
      const next = await api('/api/update');
      if (next.checked_at !== status.checked_at || next.phase !== status.phase) {
        state.bootstrap.update = next;
        renderUpdateStatus(next);
      }
    } catch (_) { /* 网络抖动时忽略，下一轮重试 */ }
  }, 30000);
}

