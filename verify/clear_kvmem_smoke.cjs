// 使用隔离真实 HTTP 源码实例；状态响应为本地替身，不访问真实 KVMem。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const BASE = process.env.NAIBA_SMOKE_BASE;
const OUTPUT = process.env.NAIBA_SMOKE_OUTPUT;

async function run(browser, viewport) {
  const context = await browser.newContext({ viewport });
  await context.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  const page = await context.newPage(), errors = [], deletionSizes = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('response', response => { if (response.status() >= 400) errors.push(`HTTP ${response.status()} ${response.url()}`); });
  page.on('dialog', dialog => dialog.accept());
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
    await page.waitForFunction(() => document.querySelector('.comfyui-clear-count').textContent.includes('260 / 260'));
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
    assert.equal(await page.locator('.comfyui-clear-count').innerText().then(text => text.startsWith('已选 0 /')), true);
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
      const core = await import('/js/01-core.js'); core.setAppMode?.('chat');
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
