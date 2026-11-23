// 使用隔离真实 HTTP 源码实例；状态响应为本地替身，不访问真实 KVMem。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const BASE = process.env.NAIBA_SMOKE_BASE;
const OUTPUT = process.env.NAIBA_SMOKE_OUTPUT;

async function kvmemCommandRaces(page, context, viewport, clean) {
  const reports = [];
  const read = async () => (await context.request.get(`${BASE}/_fixture/kvmem`)).json();
  const control = async body => {
    const response = await context.request.post(`${BASE}/_fixture/kvmem`, { data: body });
    assert.equal(response.status(), 200, await response.text());
    return response.json();
  };
  const wait = async (label, condition) => {
    let latest;
    for (let index = 0; index < 200; index++) {
      latest = await read();
      if (condition(latest)) return latest;
      await page.waitForTimeout(50);
    }
    throw Error(`${label}: ${JSON.stringify(latest)}`);
  };
  const hold = target => control({ action: 'hold', target });
  const release = target => control({ action: 'release', target });
  const setStatus = status => control({ action: 'set', status });
  const entered = target => wait(`KVMem ${target}门控未进入`, state => state.entered.some(row => row.target === target));
  const count = (state, target) => state.requests.filter(row => row.provider_id === 'kv-a' && row.target === target).length;
  const status = expected => page.waitForFunction(expected => document.querySelector('#kvmemStatusDot').dataset.status === expected, expected);
  const unlocked = () => page.waitForFunction(() => !document.querySelector('#kvmemRefresh').disabled);
  const choose = async providerId => {
    await page.evaluate(async providerId => {
      const core = await import('/js/01-core.js');
      core.setAppView('chat');
      const models = await import('/js/07-models-agents.js');
      models.applyConversationModel({ model_key: `local:${providerId}`, model_name: `${providerId}-model` });
    }, providerId);
  };
  const buttons = () => page.evaluate(() => Object.fromEntries(['Start', 'Stop', 'Refresh']
    .map(name => [name, document.getElementById(`kvmem${name}`).disabled])));
  const rapid = name => page.locator(`#kvmem${name}`).evaluate(button => {
    for (let index = 0; index < 5; index++) button.click();
  });
  await choose('kv-a');
  await status('stopped');
  await page.locator('#kvmemStatusButton').click();
  await page.waitForFunction(() => !document.querySelector('#kvmemPopover').hidden);

  // The action owns its UI state before the first status request resolves.
  const initial = await read();
  await hold('status');
  await hold('start');
  await rapid('Start');
  await entered('status');
  await page.waitForTimeout(100);
  const preflight = await read();
  const observed = { scenario: 'rapid_start_preflight', width: viewport.width,
    buttons: await buttons(), status: await page.locator('#kvmemStatusDot').getAttribute('data-status'),
    status_requests: count(preflight, 'status') - count(initial, 'status') };
  assert.deepEqual(observed.buttons, { Start: true, Stop: true, Refresh: true }, JSON.stringify(observed));
  assert.equal(observed.status, 'starting');
  assert.equal(observed.status_requests, 1, '连点五次只允许一个状态预检');
  await release('status');
  await entered('start');
  await rapid('Start');
  await rapid('Stop');
  await rapid('Refresh');
  assert.deepEqual(await buttons(), { Start: true, Stop: true, Refresh: true });
  assert.equal(count(await read(), 'start') - count(initial, 'start'), 1, '启动POST在途时重复按钮不发新命令');
  assert.equal(count(await read(), 'stop') - count(initial, 'stop'), 0);
  await page.screenshot({ path: path.join(OUTPUT, `kvmem-start-pending-${viewport.width}.png`) });
  await control({ action: 'set', status: 'error', http_status: 400, error: '控制服务暂时未响应' });
  await release('start');
  await wait('启动期间暂时400已读取', state => state.requests.slice(preflight.requests.length)
    .some(row => row.target === 'status' && row.http_status === 400));
  await status('starting');
  assert.deepEqual(await buttons(), { Start: true, Stop: true, Refresh: true });
  await control({ action: 'set', status: 'failed', raw_status: 'ready', error: '推理服务健康检查尚未就绪' });
  await wait('模型预热状态已读取', state => state.requests.slice(preflight.requests.length)
    .some(row => row.target === 'status' && row.status === 'failed' && row.raw_status === 'ready'));
  await status('starting');
  assert.deepEqual(await buttons(), { Start: true, Stop: true, Refresh: true });
  assert.equal(count(await read(), 'start') - count(initial, 'start'), 1);
  await page.screenshot({ path: path.join(OUTPUT, `kvmem-warmup-${viewport.width}.png`) });
  await setStatus('ready');
  await status('ready');
  await unlocked();
  await clean();
  reports.push({ ...observed, passed: true });
  reports.push({ scenario: 'transient_warmup', passed: true, start_posts: 1 });

  const beforeStop = await read();
  await hold('status');
  await hold('stop');
  await rapid('Stop');
  await entered('status');
  assert.deepEqual(await buttons(), { Start: true, Stop: true, Refresh: true });
  await status('stopping');
  assert.equal(count(await read(), 'status') - count(beforeStop, 'status'), 1);
  await release('status');
  await entered('stop');
  await rapid('Start');
  await rapid('Stop');
  assert.equal(count(await read(), 'stop') - count(beforeStop, 'stop'), 1);
  await release('stop');
  await status('stopped');
  await unlocked();
  await clean();
  reports.push({ scenario: 'rapid_stop', passed: true });

  // A refresh captured before Start cannot overwrite the operation's starting state.
  await hold('status');
  await page.evaluate(async () => { void (await import('/js/07-models-agents.js')).refreshKvmemStatus({ quiet: true }); });
  await entered('status');
  await hold('start');
  await page.locator('#kvmemStart').click();
  await status('starting');
  await release('status');
  await entered('start');
  await page.waitForTimeout(150);
  assert.equal(await page.locator('#kvmemStatusDot').getAttribute('data-status'), 'starting');
  assert.deepEqual(await buttons(), { Start: true, Stop: true, Refresh: true });
  await page.screenshot({ path: path.join(OUTPUT, `kvmem-stale-refresh-${viewport.width}.png`) });
  await setStatus('ready');
  await release('start');
  await status('ready');
  await unlocked();
  await clean();
  reports.push({ scenario: 'refresh_before_start', passed: true });

  // If the service changed after the last UI refresh, an already-satisfied command sends no POST.
  await page.locator('#kvmemStop').click();
  await status('stopped');
  await unlocked();
  const beforeReady = await read();
  await setStatus('ready');
  await page.locator('#kvmemStart').click();
  await status('ready');
  await unlocked();
  assert.equal(count(await read(), 'start'), count(beforeReady, 'start'), '实际服务已就绪时不重复启动');
  await clean();
  reports.push({ scenario: 'already_ready', passed: true });

  // A pending A command remains locked across A -> B -> A until its own POST settles.
  await page.locator('#kvmemStop').click();
  await status('stopped');
  await unlocked();
  const beforeRoundTrip = await read();
  await hold('start');
  await page.locator('#kvmemStart').click();
  await entered('start');
  await choose('kv-b');
  await status('ready');
  await choose('kv-a');
  await status('starting');
  assert.deepEqual(await buttons(), { Start: true, Stop: true, Refresh: true });
  await rapid('Start');
  await rapid('Stop');
  assert.equal(count(await read(), 'start') - count(beforeRoundTrip, 'start'), 1);
  await page.screenshot({ path: path.join(OUTPUT, `kvmem-provider-roundtrip-${viewport.width}.png`) });
  await setStatus('ready');
  await release('start');
  await status('ready');
  await unlocked();
  assert.equal(await page.locator('#kvmemSummary').textContent().then(text => text.includes('kv-a-model')), true);
  await clean();
  reports.push({ scenario: 'provider_roundtrip', passed: true, start_posts: 1 });
  fs.writeFileSync(path.join(OUTPUT, `kvmem-command-report-${viewport.width}.json`), JSON.stringify(reports, null, 2));
  console.log(`KVMem ${viewport.width}px：启动/停止连点、预检锁、预热状态、迟到刷新、已就绪去重与切卡往返通过`);
}

async function run(browser, viewport) {
  const context = await browser.newContext({ viewport });
  await context.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  const page = await context.newPage(), errors = [], deletionSizes = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() !== 'error') return;
    // 资源加载失败已由上面的 response 监听统一收集（带 URL、可豁免 WIP 端点），这条是重复信号。
    if (/Failed to load resource/.test(message.text())) return;
    errors.push(message.text());
  });
  page.on('response', response => {
    if (response.status() < 400) return;
    if (process.env.NAIBA_KVMEM_ACTION_ONLY && response.status() === 400
        && /\/api\/providers\/[^/]+\/kvmem\/status$/.test(new URL(response.url()).pathname)) return;
    // 并行 dream_* 会话 WIP：前端 24-comfyui.js 已请求任务事件流，后端 /events 路由未跟上。
    // 它补齐路由后 404 自然消失，届时移除这条豁免。
    if (/\/api\/comfyui\/tasks\/[^/]+\/events/.test(response.url())) return;
    errors.push(`HTTP ${response.status()} ${response.url()}`);
  });
  let dialogCount = 0;
  page.on('dialog', dialog => { dialogCount += 1; dialog.accept(); });
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/comfyui/tasks' && request.method() === 'DELETE') {
      deletionSizes.push(request.postDataJSON().task_ids.length);
    }
  });
  const clean = async () => {
    assert.deepEqual(errors, []);
    assert.equal(await page.locator('body').evaluate(node => /JS ERROR|PROMISE REJECT/.test(node.innerText)), false);
  };
  const clear = async () => {
    await page.locator('#comfyuiTasksSection details').evaluate(node => { node.open = true; });
    await page.locator('#comfyuiClearTasks').click();
    await page.waitForFunction(() => !document.querySelector('#comfyuiClearBar').hidden);
    await clean();
  };
  const waitIdle = () => page.waitForFunction(() => !document.querySelector('#comfyuiClearDelete').disabled);
  try {
    await context.request.post(`${BASE}/_fixture/reset`, { data: {} });
    await page.goto(BASE, { waitUntil: 'networkidle' });
    if (process.env.NAIBA_KVMEM_ACTION_ONLY) {
      await kvmemCommandRaces(page, context, viewport, clean);
      return;
    }
    await page.evaluate(async () => {
      const studio = await import('/js/22-studio.js'); studio.openStudioView(); studio.setStudioPage('comfyui');
    });
    await page.locator('#comfyuiPanes [data-comfyui-pane="tasks"]').click();
    await clean();
    // 先只勾一条，取消不发删除请求。
    await clear();
    const checkbox = page.locator('[data-comfyui-task-check]').first();
    await checkbox.check(); await waitIdle();
    await page.locator('#comfyuiClearCancel').click();
    assert.equal(deletionSizes.length, 0);
    await clear();
    // 不靠模块内私有变量：由真实分页按钮加载全部 261 条单次历史。
    for (let i = 0; i < 40; i += 1) {
      if (await page.locator('#comfyuiTaskHistoryFooter').innerText().then(text => text.includes('已加载全部'))) break;
      await page.locator('#comfyuiTaskList').evaluate(host => { host.scrollTop = host.scrollHeight; host.dispatchEvent(new Event('scroll')); });
      const more = page.locator('#comfyuiTaskHistoryFooter button');
      if (await more.isVisible()) await more.evaluate(button => { if (!button.disabled) button.click(); });
      await page.waitForTimeout(100);
      await clean();
    }
    await page.waitForFunction(() => document.querySelector('#comfyuiTaskHistoryFooter').innerText.includes('已加载全部'));
    await page.locator('#comfyuiClearSelectAll').click();
    await page.waitForFunction(() => document.querySelector('#comfyuiClearBar .comfyui-clear-count').textContent.includes('260 / 260'));
    await page.locator('#comfyuiClearDelete').click();
    await page.waitForFunction(() => document.querySelector('#comfyuiClearBar').hidden);
    assert.deepEqual(deletionSizes, [200, 60]);
    const state = await (await context.request.get(`${BASE}/_fixture/state`)).json();
    assert.equal(state.single, 1, '活动单次保留'); assert.equal(state.batch, 2, '批量任务保留');
    await clean();

    // 旧删除回包返回前切档再进入，仍持请求锁，新模式不被旧请求关闭。
    await context.request.post(`${BASE}/_fixture/reset`, { data: {} });
    await page.reload({ waitUntil: 'networkidle' });
    await page.evaluate(async () => {
      const studio = await import('/js/22-studio.js'); studio.openStudioView(); studio.setStudioPage('comfyui');
    });
    await page.locator('#comfyuiPanes [data-comfyui-pane="tasks"]').click();
    await clear();
    await page.locator('[data-comfyui-task-check]').first().check();
    let release, intercepted;
    const hit = new Promise(resolve => { intercepted = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    await page.route('**/api/comfyui/tasks', async route => {
      if (route.request().method() !== 'DELETE') return route.continue();
      intercepted(); await gate;
      const response = await route.fetch(); await route.fulfill({ response });
    });
    await page.locator('#comfyuiClearDelete').click(); await hit;
    await page.locator('[data-comfyui-results-seg="batch"]').click();
    await page.locator('[data-comfyui-results-seg="single"]').click(); await clear();
    assert.equal(await page.locator('[data-comfyui-task-check]').first().isDisabled(), true);
    release();
    await page.waitForFunction(() => !document.querySelector('#comfyuiClearSelectAll').disabled);
    assert.equal(await page.locator('#comfyuiClearBar').isVisible(), true);
    assert.equal(await page.locator('#comfyuiClearBar .comfyui-clear-count').innerText().then(text => text.startsWith('已选 0 /')), true);
    await page.unroute('**/api/comfyui/tasks');
    await clean();

    // 真实模块经当前会话绑定切换 KVMem；旧回包逆序不污染新控件。
    let releaseA, startedA;
    const gotA = new Promise(resolve => { startedA = resolve; });
    const delayedA = new Promise(resolve => { releaseA = resolve; });
    await page.route('**/api/providers/kv-a/kvmem/status', async route => {
      startedA(); await delayedA;
      await route.fulfill({ json: { status: 'stopped', model: 'A-old' } });
    });
    await page.evaluate(async () => {
      const core = await import('/js/01-core.js'); core.setAppView('chat');
      const models = await import('/js/07-models-agents.js');
      models.applyConversationModel({ model_key: 'local:kv-a', model_name: 'kv-a-model' });
    });
    await gotA;
    await page.evaluate(async () => (await import('/js/07-models-agents.js'))
      .applyConversationModel({ model_key: 'local:kv-b', model_name: 'kv-b-model' }));
    await page.waitForFunction(() => document.querySelector('#kvmemStatusDot').dataset.status === 'ready');
    releaseA(); await page.waitForTimeout(120);
    assert.equal(await page.locator('#kvmemSummary').textContent().then(text => text.includes('kv-b-model')), true);
    assert.equal(await page.locator('#kvmemStart').isDisabled(), true);
    await page.unroute('**/api/providers/kv-a/kvmem/status');
    await clean();

    // KVMem 完整启停链路：切到 kv-a（stopped）→ 启动确认 → 轮询 starting→ready → 停止轮询 stopped。
    await page.evaluate(async () => {
      const models = await import('/js/07-models-agents.js');
      models.applyConversationModel({ model_key: 'local:kv-a', model_name: 'kv-a-model' });
    });
    await page.waitForFunction(() => document.querySelector('#kvmemStatusDot').dataset.status === 'stopped');
    await page.locator('#kvmemStatusButton').click();
    await page.waitForFunction(() => !document.querySelector('#kvmemPopover').hidden);
    // 点控制台外自动收起；再点按钮重开后继续启动链路。
    await page.locator('#messageInput').click();
    await page.waitForFunction(() => document.querySelector('#kvmemPopover').hidden);
    await page.locator('#kvmemStatusButton').click();
    await page.waitForFunction(() => !document.querySelector('#kvmemPopover').hidden);
    dialogCount = 0;
    await page.locator('#kvmemStart').click();
    assert.equal(dialogCount, 0, '顶栏启动不再弹确认框');
    await page.waitForFunction(() => document.querySelector('#kvmemStatusDot').dataset.status === 'ready', null, { timeout: 30000 });
    assert.equal(await page.locator('#kvmemSummary').textContent().then(text => text.includes('kv-a-model')), true);
    await clean();
    await page.locator('#kvmemStop').click();
    await page.waitForFunction(() => document.querySelector('#kvmemStatusDot').dataset.status === 'stopped', null, { timeout: 30000 });
    await clean();

    // 设置表单：kv-a 卡反显完整配置、KVMem 独立预设（格式字段隐藏、随预设展开）；capture 回填；保存后重开仍在。
    await page.evaluate(async () => {
      const settings = await import('/js/09-settings.js');
      settings.openProviderCard('kv-a');
    });
    await page.waitForSelector('#providerDialog[open]');
    assert.equal(await page.locator('#providerKvmemEnabled').isChecked(), true);
    assert.equal(await page.locator('#kvmemProviderFields').isHidden(), false);
    assert.equal(await page.locator('#providerKvmemInstallDir').inputValue(), 'D:/kvmem-fixture');
    assert.equal(await page.locator('#providerKvmemModelPath').inputValue(), 'D:/models/kv-a.gguf');
    assert.equal(await page.locator('#providerKvmemBackend').inputValue(), 'ninfer');
    assert.equal(await page.locator('#providerFormatField').isHidden(), true);
    // 勾选卡内直接启停：服务行可见、状态自动带出（stopped），走一轮启动→就绪→停止→未运行。
    assert.equal(await page.locator('#providerKvmemServiceRow').isHidden(), false);
    await page.waitForFunction(() => document.querySelector('#providerKvmemServiceText').textContent.includes('未运行'));
    assert.equal(await page.locator('#providerKvmemServiceStart').isDisabled(), false);
    dialogCount = 0;
    await page.locator('#providerKvmemServiceStart').click();
    assert.equal(dialogCount, 0, '设置页启动不再弹确认框');
    await page.waitForFunction(() => document.querySelector('#providerKvmemServiceText').textContent.includes('就绪'), null, { timeout: 30000 });
    await page.locator('#providerKvmemServiceStop').click();
    await page.waitForFunction(() => document.querySelector('#providerKvmemServiceText').textContent.includes('未运行'), null, { timeout: 30000 });
    assert.equal(await page.locator('#providerKvmemServiceStop').isDisabled(), true);
    await clean();
    // KVMem 是独立预设项：格式字段隐藏；点别的本地预设再点回 KV，区块随预设展开/收起。
    await page.evaluate(async () => {
      const settings = await import('/js/09-settings.js');
      settings.applyProviderPreset('llama_cpp');
    });
    await page.waitForFunction(() => !document.querySelector('#providerFormatField').hidden);
    assert.equal(await page.locator('#kvmemProviderFields').isHidden(), true);
    await page.evaluate(async () => {
      const settings = await import('/js/09-settings.js');
      settings.applyProviderPreset('kvmem');
    });
    await page.waitForFunction(() => !document.querySelector('#kvmemProviderFields').hidden);
    assert.equal(await page.locator('#providerFormatField').isHidden(), true);
    // 回到 kv-a 已存卡继续走 capture/保存链路。
    await page.evaluate(async () => {
      const settings = await import('/js/09-settings.js');
      settings.openProviderCard('kv-a');
    });
    assert.equal(await page.locator('#providerKvmemBackend').inputValue(), 'ninfer');
    // 预存配置没有 VisionResidency 键：反显落 CPU 默认。
    assert.equal(await page.locator('#providerKvmemVisionResidency').inputValue(), 'cpu');
    await page.locator('#providerKvmemCapture').click();
    await page.waitForFunction(() => document.querySelector('#providerKvmemModelPath').value === 'D:/models/captured.gguf', null, { timeout: 10000 });
    assert.equal(await page.locator('#providerKvmemConfigJson').inputValue().then(text => text.includes('"Context": 131072')), true);
    assert.equal(await page.locator('#providerKvmemCaptureHint').textContent().then(text => text.includes('已导入')), true);
    // capture 替身带 VisionResidency=resident：导入后下拉同步。
    assert.equal(await page.locator('#providerKvmemVisionResidency').inputValue(), 'resident');
    await page.locator('#saveProvider').click();
    await page.waitForFunction(() => !document.querySelector('#providerDialog').open, null, { timeout: 15000 });
    await clean();
    await page.evaluate(async () => {
      const settings = await import('/js/09-settings.js');
      settings.openProviderCard('kv-a');
    });
    await page.waitForFunction(() => document.querySelector('#providerKvmemModelPath')?.value === 'D:/models/captured.gguf', null, { timeout: 10000 });
    assert.equal(await page.locator('#providerKvmemInstallDir').inputValue(), 'D:\\kvmem-fixture');
    // 保存后显式写入了视觉驻留键（capture 导入值优先于草稿默认）。
    assert.equal(await page.locator('#providerKvmemConfigJson').inputValue().then(text => text.includes('"VisionResidency": "resident"')), true);
    await page.evaluate(async () => {
      const settings = await import('/js/09-settings.js');
      settings.cancelProviderEdit();
    });
    await clean();
    await page.screenshot({ path: path.join(OUTPUT, `clear-kvmem-${viewport.width}.png`) });
    console.log(`清除/KVMem ${viewport.width}px：分块260条、取消、切档竞态及状态逆序通过`);
  } finally { await context.close(); }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try { for (const viewport of [{ width: 1280, height: 900 }, { width: 375, height: 812 }]) await run(browser, viewport); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
