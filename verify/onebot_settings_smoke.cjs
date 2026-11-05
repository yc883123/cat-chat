// QQ 设置冒烟：仅对隔离源码实例运行；真实保存/监听/假 NapCat 连接/新建与已有会话绑定。
// 不访问真实 QQ 或模型；局域网拒绝与保存失败分支使用浏览器路由注入。
// 先起 verify/_serve_tmp.py，再设置 NAIBA_SMOKE_BASE 后运行本脚本。
const assert = require('node:assert/strict');
const net = require('node:net');
const path = require('node:path');
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const TOKEN = 'onebot-ui-smoke-token';
const QQ = '20000123';
const BOT = '10000123';

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function waitFor(check, label) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`等待超时：${label}`);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await context.newPage();
  const errors = [];
  const createdConversations = [];
  let original;
  let changed = false;
  let ws;
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  const request = async (endpoint, options = {}) => {
    const response = await context.request.fetch(`${BASE}${endpoint}`, options);
    const body = await response.json();
    assert(response.ok(), `${endpoint}: ${body.error || response.status()}`);
    return body;
  };
  try {
    original = (await request('/api/onebot/status')).config;
    assert(!original.enabled && !original.has_access_token, '只能使用未启用 QQ 且未配置 OneBot 口令的隔离实例');
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await page.evaluate(() => { document.querySelector('#onboardingDialog')?.close(); });
    await page.click('#openSettings');
    let firstStatusFails = true;
    await page.route('**/api/onebot/status', (route) => {
      if (!firstStatusFails) return route.continue();
      firstStatusFails = false;
      return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: '首次状态读取失败测试' }) });
    });
    await page.click('[data-settings-tab="onebot"]');
    await page.locator('#onebotConfigFields').waitFor();
    await waitFor(async () => (await page.locator('#onebotFeedback').innerText()).includes('首次状态读取失败测试'), '首次读取错误');
    assert(await page.locator('#onebotSave').isDisabled());
    await page.click('#onebotRefresh');
    await waitFor(() => page.locator('#onebotEnabled').isEnabled(), 'QQ 设置载入');
    assert.equal(await page.inputValue('#onebotListenHost'), original.listen_host, '首次失败后成功刷新须回填配置');
    assert.equal(await page.inputValue('#onebotListenPort'), String(original.listen_port));
    assert.equal(await page.locator('#onebotTokenStatus').innerText(), '未配置');
    await page.unroute('**/api/onebot/status');
    const port = await freePort();
    await page.fill('#onebotListenHost', '127.0.0.1');
    await page.fill('#onebotListenPort', String(port));
    await page.fill('#onebotAccessToken', TOKEN);
    await page.fill('#onebotAllowedUsers', `${QQ}，${QQ}`);
    await page.check('#onebotEnabled');
    changed = true;
    await page.click('#onebotSave');
    await waitFor(async () => (await page.locator('#onebotFeedback').innerText()).includes('已保存'), '保存真实 QQ 设置');
    await waitFor(async () => (await page.locator('#onebotListening').innerText()) === '已监听', '实际端口监听');
    assert.equal(await page.inputValue('#onebotAccessToken'), '');
    assert.equal(await page.locator('#onebotTokenStatus').innerText(), '已配置');
    let snapshot = await request('/api/onebot/status');
    assert.deepEqual(snapshot.config.allowed_user_ids, [QQ]);
    assert.equal(snapshot.config.access_token, undefined);
    const address = await page.inputValue('#onebotAddress');
    assert.equal(address, `ws://127.0.0.1:${port}/onebot/v11/ws`);
    await page.click('#onebotCopyAddress');
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), address);

    ws = new WebSocket(`${address}?access_token=${encodeURIComponent(TOKEN)}`);
    await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', () => reject(new Error('假 NapCat 连接失败')), { once: true }); });
    ws.send(JSON.stringify({ post_type: 'meta_event', meta_event_type: 'lifecycle', sub_type: 'connect', self_id: Number(BOT) }));
    await waitFor(async () => (await request('/api/onebot/status')).status.account_id === BOT, '假 NapCat 机器人账号');
    await page.click('#onebotRefresh');
    await waitFor(async () => (await page.locator('#onebotConnected').innerText()) === '已连接', '真实连接状态回显');

    // 空口令保存必须保留原口令；重连成功是服务端保留口令的行为证据。
    ws.close();
    await page.fill('#onebotAllowedUsers', `${QQ}\n20000456`);
    await page.click('#onebotSave');
    await waitFor(async () => (await request('/api/onebot/status')).config.allowed_user_ids.length === 2, '保留口令保存白名单');
    await waitFor(async () => (await page.locator('#onebotFeedback').innerText()).includes('已保存')
      && await page.locator('#onebotSave').isEnabled(), '白名单保存及监听重启完成');
    assert.equal((await request('/api/onebot/status')).config.has_access_token, true);
    ws = new WebSocket(`${address}?access_token=${encodeURIComponent(TOKEN)}`);
    await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', () => reject(new Error('保存后原口令失效')), { once: true }); });

    await page.fill('#onebotBotId', BOT);
    await page.fill('#onebotUserId', QQ);
    await page.selectOption('#onebotBindMode', 'new');
    await page.fill('#onebotNewTitle', 'QQ 设置真实绑定冒烟');
    await page.click('#onebotBind');
    await waitFor(async () => (await request('/api/onebot/status')).bindings.some((item) => item.qq_user_id === QQ), '真实新建会话绑定');
    snapshot = await request('/api/onebot/status');
    const binding = snapshot.bindings.find((item) => item.qq_user_id === QQ);
    createdConversations.push(binding.conversation_id);
    await waitFor(async () => (await page.locator('#onebotBindings').innerText()).includes('QQ 设置真实绑定冒烟'), '绑定列表回显标题');
    await page.locator('#onebotBindings [data-onebot-unbind]').first().click();
    await waitFor(async () => !(await request('/api/onebot/status')).bindings.some((item) => item.qq_user_id === QQ), '真实解绑');
    assert((await request(`/api/conversations/${binding.conversation_id}`)).id, '解绑保留会话');
    await page.selectOption('#onebotBindMode', 'existing');
    await page.selectOption('#onebotConversationId', binding.conversation_id);
    await page.click('#onebotBind');
    await waitFor(async () => (await request('/api/onebot/status')).bindings.some((item) => item.conversation_id === binding.conversation_id), '真实绑定已有会话');

    await page.fill('#onebotAllowedUsers', `${QQ}\n20000999`);
    await page.click('#onebotRefresh');
    await page.waitForTimeout(200);
    assert.equal(await page.inputValue('#onebotAllowedUsers'), `${QQ}\n20000999`, '刷新状态不覆盖未保存草稿');
    await page.route('**/api/settings', (route) => route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: '保存失败测试' }) }));
    await page.click('#onebotSave');
    await waitFor(async () => (await page.locator('#onebotFeedback').innerText()).includes('保存失败测试'), '显示保存失败');
    await page.unroute('**/api/settings');

    // 会话列表慢请求期间，解绑身份必须与界面同步，不能沿用数组序号误删另一行。
    const actualSnapshot = await request('/api/onebot/status');
    const firstBinding = actualSnapshot.bindings.find((item) => item.qq_user_id === QQ);
    const secondBinding = { ...firstBinding, qq_user_id: '20000456' };
    let projectedBindings = [firstBinding, secondBinding];
    await page.route('**/api/onebot/status', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...actualSnapshot, bindings: projectedBindings }) }));
    await page.click('#onebotRefresh');
    await waitFor(async () => (await page.locator('#onebotBindings [data-onebot-unbind]').count()) === 2, '两条绑定投影');
    let releaseConversations;
    let conversationsWaiting = false;
    await page.route('**/api/conversations', async route => {
      conversationsWaiting = true;
      await new Promise(resolve => { releaseConversations = resolve; });
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ conversations: [] }) });
    });
    const projectedUnbinds = [];
    await page.route('**/api/onebot/unbind', route => {
      projectedUnbinds.push(route.request().postDataJSON());
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ removed: true }) });
    });
    projectedBindings = [secondBinding];
    await page.click('#onebotRefresh');
    await waitFor(() => conversationsWaiting, '会话列表慢请求已开始');
    assert.equal(await page.locator('#onebotBindings [data-onebot-unbind]').count(), 1, '慢请求期间立即同步绑定列表');
    assert((await page.locator('#onebotBindings').innerText()).includes('20000456'));
    await page.locator('#onebotBindings [data-onebot-unbind]').first().click();
    await waitFor(() => projectedUnbinds.length === 1, '稳定身份解绑请求');
    assert.equal(projectedUnbinds[0].qq_user_id, '20000456');
    releaseConversations();
    await page.unroute('**/api/conversations');
    await page.unroute('**/api/onebot/unbind');
    await page.unroute('**/api/onebot/status');
    await page.click('#onebotRefresh');
    await waitFor(async () => (await page.locator('#onebotBindings').innerText()).includes(QQ), '恢复真实绑定投影');

    // 保存发出后关闭重开：旧响应不能回填新周期，也不能越过新周期的权限失败。
    let releaseSave;
    let saveWaiting = false;
    await page.route('**/api/settings', async route => {
      saveWaiting = true;
      await new Promise(resolve => { releaseSave = resolve; });
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ settings: { onebot: { ...actualSnapshot.config, listen_port: 19999 } } }) });
    });
    await page.fill('#onebotAccessToken', 'closing-secret-test');
    await page.click('#onebotSave');
    await waitFor(() => saveWaiting, '旧保存请求等待响应');
    await page.evaluate(() => document.querySelector('#settingsDialog').close());
    assert.equal(await page.inputValue('#onebotAccessToken'), '', '关闭时清空口令草稿');
    await page.route('**/api/onebot/status', route => route.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify({ error: '重新打开后无管理权限测试' }) }));
    await page.click('#openSettings');
    await waitFor(async () => (await page.locator('#onebotFeedback').innerText()).includes('重新打开后无管理权限测试'), '新周期权限错误');
    releaseSave();
    await page.waitForTimeout(250);
    assert(await page.locator('#onebotSave').isDisabled());
    assert(await page.locator('#onebotBind').isDisabled());
    assert.notEqual(await page.inputValue('#onebotListenPort'), '19999', '旧保存不得回填已失效表单');
    assert((await page.locator('#onebotFeedback').innerText()).includes('重新打开后无管理权限测试'));
    await page.unroute('**/api/settings');
    await page.unroute('**/api/onebot/status');
    await page.click('#onebotRefresh');
    await waitFor(() => page.locator('#onebotSave').isEnabled(), '权限恢复后重新启用');

    for (const viewport of [{ name: 'desktop', width: 1280, height: 900 }, { name: 'mobile', width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      await page.locator('[data-settings-tab="onebot"]').scrollIntoViewIfNeeded();
      const overflow = await page.locator('[data-settings-panel="onebot"]').evaluate((panel) => panel.scrollWidth > panel.clientWidth + 2);
      assert(!overflow, `${viewport.name} QQ 面板横向溢出`);
      await page.screenshot({ path: path.join(__dirname, `onebot_settings_${viewport.name}.png`) });
    }
    await page.route('**/api/onebot/status', (route) => route.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify({ error: 'QQ 渠道状态只能在本机查看' }) }));
    await page.click('#onebotRefresh');
    await waitFor(async () => (await page.locator('#onebotFeedback').innerText()).includes('只能在本机查看'), '非本机访问提示');
    assert(await page.locator('#onebotSave').isDisabled());
    assert(await page.locator('#onebotBind').isDisabled());
    await page.unroute('**/api/onebot/status');
    assert.deepEqual(errors, []);
    console.log('PASS QQ 设置：桌面/375px、真实保存与监听、假 NapCat、口令脱敏、地址复制、会话绑定、首次失败恢复、稳定身份解绑、关闭重开隔离、草稿保留');
  } finally {
    if (ws) ws.close();
    await page.close();
    if (changed) {
      const snapshot = await request('/api/onebot/status');
      for (const item of snapshot.bindings.filter((item) => item.qq_user_id === QQ && item.channel_account_id === BOT)) {
        await request('/api/onebot/unbind', { method: 'POST', data: { account_id: BOT, qq_user_id: QQ, scope: item.scope, qq_group_id: item.qq_group_id } });
      }
      await request('/api/settings', { method: 'POST', data: { onebot: { ...original, clear_access_token: true } } });
      for (const id of createdConversations) await request(`/api/conversations/${id}`, { method: 'DELETE' });
    }
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
