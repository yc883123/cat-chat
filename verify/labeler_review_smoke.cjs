// 真源码页面 + 真实设置 API；仅任务结果和模型目录时序用可控替身，无付费调用。
// 配套服务：verify/_serve_labeler_review.py；NAIBA_SMOKE_BASE 可覆盖端口。
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8807';
const key = name => `online:review-${name}`;
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

async function run(browser, viewport) {
  const page = await browser.newPage({ viewport });
  const errors = [];
  const itemOffsets = [];
  const lateModel = deferred();
  const earlySettings = deferred();
  const releaseSettings = deferred();
  let terminal = false;
  let retryCount = 0;
  let lateModelSeen = false;
  let settingsHeld = false;
  const item = (human = false) => ({
    item_id: human ? 'human-item' : 'review-item', name: 'review.png',
    relative_path: 'review.png', state: human ? 'human_check' : terminal ? 'succeeded' : 'running',
    annotation_id: !human && terminal ? 1 : null,
    worker_model_name: 'original-author-model', worker_slot: -1,
    supervisor_decision: human ? 'human_check' : terminal ? 'reused' : '',
    candidate: human ? { tags: ['candidate'], description: '待检查' } : null,
  });
  const task = (id = 'review-task') => {
    const human = id === 'human-task';
    return { id, label: human ? '人工检查批次' : '终态刷新批次',
      status: human ? 'failed' : terminal ? 'done' : 'running',
      concurrency_enabled: true, worker_count: 2,
      progress: { total: 1, done: !human && terminal ? 1 : 0, human_check: human ? 1 : 0 },
      can_continue: human, can_retry_failed: false,
      items: !human && !terminal ? [item()] : [], items_total: 1,
      input: { config: { write_txt: false } },
    };
  };
  const check = () => assert.deepEqual(errors, [], '页面不能出现脚本或请求错误');
  const selectPane = async pane => {
    const button = page.locator(`#labelerPanes [data-labeler-pane="${pane}"]`);
    if (await button.isVisible()) await button.click(); // 桌面三栏同时展示，无需切手机分档。
  };
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  page.on('response', response => {
    const pathname = new URL(response.url()).pathname;
    // 首次重试故意失败，用来验证按钮可恢复；其余 HTTP 错误一律不放过。
    if (response.status() >= 400 && !pathname.endsWith('/favicon.ico')
        && !(pathname === '/api/labeler/tasks/human-task/retry' && response.status() === 409)) {
      errors.push(`HTTP ${response.status()} ${pathname}`);
    }
  });
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  await page.route('**/api/providers/models', async route => {
    const provider = route.request().postDataJSON().model_key;
    const name = provider.split('-').at(-1);
    if (name === 'C') { lateModelSeen = true; await lateModel.promise; }
    await route.fulfill({ json: { models: [{ id: `${name}-model`, name: `${name}-model` }] } });
  });
  await page.route('**/api/labeler/settings', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const body = route.request().postDataJSON();
    assert.deepEqual(Object.keys(body), ['provider_limits_patch']);
    assert.equal(Object.keys(body.provider_limits_patch).length, 1);
    const response = await route.fetch(); // 真服务执行锁内补丁。
    if (Object.hasOwn(body.provider_limits_patch, key('A')) && !settingsHeld) {
      settingsHeld = true;
      earlySettings.resolve();
      await releaseSettings.promise; // 让旧的整份响应晚于另一供应商回包。
    }
    await route.fulfill({ response });
  });
  await page.route('**/api/labeler/tasks**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/labeler/tasks') {
      return route.fulfill({ json: { tasks: [task(), task('human-task')] } });
    }
    if (url.pathname.endsWith('/retry')) {
      assert.deepEqual(route.request().postDataJSON(), { mode: 'unfinished', confirm_uncertain: false });
      retryCount += 1;
      return route.fulfill(retryCount === 1
        ? { status: 409, json: { error: '测试暂时不可用' } }
        : { json: { task: task('continued-task') } });
    }
    const id = url.pathname.split('/')[4];
    if (url.pathname.endsWith('/items')) {
      const offset = Number(url.searchParams.get('offset'));
      itemOffsets.push({ id, offset });
      return route.fulfill({ json: { items: offset === 0 ? [item(id === 'human-task')] : [], total: 1 } });
    }
    return route.fulfill({ json: { task: task(id) } });
  });
  await page.route('**/api/labeler/annotations?**', route => route.fulfill({ json: {
    annotations: terminal ? [{ id: 1, tags: ['review-ok'], description: '终态实际标注已经刷新' }] : [],
    total: terminal ? 1 : 0,
  } }));
  try {
    const reset = await page.request.post(`${BASE}/api/labeler/settings`, { data: { provider_limits: {} } });
    assert.equal(reset.ok(), true, '每个视口使用独立的初始额度状态');
    await page.goto(BASE, { waitUntil: 'load' });
    await page.waitForTimeout(600); // 等待服务端保存的顶层视图恢复完成。
    const openSidebar = page.locator('button[aria-label="打开导航"]:visible').first();
    if (await openSidebar.count()) await openSidebar.click();
    await page.click('#openStudio');
    await page.click('[data-studio-page="labeler"]');
    await page.waitForFunction(() => document.querySelector('#labelerModelSelect')?.value === 'A-model');
    await page.click('[data-labeler-page-tab="custom"]');
    await page.check('#labelerConcurrencyEnabled');
    await page.waitForFunction(() => document.querySelector('#labelerSupervisorModel')?.value === 'A-model');
    check();

    await page.selectOption('#labelerSupervisorProvider', key('C'));
    await page.waitForTimeout(100);
    assert.equal(lateModelSeen, true);
    await page.selectOption('#labelerSupervisorProvider', key('D'));
    await page.waitForFunction(() => document.querySelector('#labelerSupervisorModel')?.value === 'D-model');
    lateModel.resolve();
    await page.waitForTimeout(200);
    assert.equal(await page.inputValue('#labelerSupervisorModel'), 'D-model');
    check();

    await page.locator('#labelerProviderLimits summary').click();
    const limitA = page.locator(`[data-limit-key="${key('A')}"]`);
    const limitD = page.locator(`[data-limit-key="${key('D')}"]`);
    await limitA.fill('1');
    await limitA.dispatchEvent('change');
    await earlySettings.promise;
    await limitD.fill('2');
    const savedD = page.waitForResponse(response => response.url().endsWith('/api/labeler/settings')
      && response.request().method() === 'POST'
      && Object.hasOwn(response.request().postDataJSON().provider_limits_patch, key('D')));
    await limitD.dispatchEvent('change');
    await savedD;
    releaseSettings.resolve();
    await page.waitForFunction(() => document.querySelector('#labelerTaskHint').textContent.includes('测试 A'));
    assert.equal(await limitA.inputValue(), '1');
    assert.equal(await limitD.inputValue(), '2');
    const settings = await (await page.request.get(`${BASE}/api/labeler/settings`)).json();
    assert.equal(settings.provider_limits[key('A')], 1);
    assert.equal(settings.provider_limits[key('D')], 2);
    check();

    terminal = true;
    await selectPane('results');
    await page.waitForFunction(() => document.querySelector('#labelerResultBody').textContent.includes('终态实际标注已经刷新'));
    assert.equal(await page.locator('.labeler-anno').count(), 1);
    assert.equal(await page.locator('.labeler-anno-state').textContent(), '通过');
    assert(itemOffsets.some(row => row.id === 'review-task' && row.offset === 0));
    const meta = await page.locator('.labeler-anno-meta').textContent();
    assert(meta.includes('original-author-model') && !meta.includes('#0') && !meta.includes('#1'));
    check();

    await selectPane('queue');
    await page.click('[data-labeler-task="human-task"]');
    await selectPane('config');
    await page.locator('#labelerContinue').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#labelerRetryModelFailed').isVisible(), false);
    await page.click('#labelerContinue');
    await page.waitForFunction(() => document.querySelector('#labelerTaskHint').textContent.includes('测试暂时不可用'));
    assert.equal(await page.locator('#labelerContinue').isEnabled(), true);
    await page.click('#labelerContinue');
    await page.waitForFunction(() => document.querySelector('#labelerTaskHint').textContent.includes('已创建继续任务'));
    assert.equal(retryCount, 2);
    check();
    await page.screenshot({ path: path.join(__dirname, `labeler_review_${viewport.width}.png`) });
    console.log(JSON.stringify({ viewport: viewport.width, terminalRefresh: true, supervisorRace: true,
      settingsPatch: true, continueRetry: true, cachedAuthor: true, errors }));
  } finally {
    lateModel.resolve();
    releaseSettings.resolve();
    await page.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 375, height: 812 }]) await run(browser, viewport);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
