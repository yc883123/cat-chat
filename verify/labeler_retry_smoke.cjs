// 真实页面/HTTP/SkillAgent/runner/数据库/导出；仅模型 complete 为本地替身。
// 运行项目虚拟环境 Python verify/_serve_labeler_retry.py，自带隔离服务和 100 张图片。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE;
const OUTPUT = process.env.NAIBA_SMOKE_OUTPUT;
const INPUTS = process.env.NAIBA_SMOKE_INPUTS;

async function run(browser, viewport, mode) {
  const page = await browser.newPage({ viewport, acceptDownloads: true });
  const errors = [];
  const posts = [];
  const checkNoScriptError = async () => {
    assert.deepEqual(errors, [], '页面不能出现脚本或请求错误');
    const text = await page.locator('body').innerText();
    assert.ok(!text.includes('JS ERROR') && !text.includes('PROMISE REJECT'), '页面不能出现 JS 错误条');
  };
  const pane = async name => {
    const button = page.locator(`#labelerPanes [data-labeler-pane="${name}"]`);
    if (await button.isVisible()) await button.click();
  };
  const json = async url => {
    const response = await page.request.get(`${BASE}${url}`);
    assert.ok(response.ok(), `${url}: HTTP ${response.status()}`);
    return response.json();
  };
  const until = async (probe, message) => {
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      await checkNoScriptError();
      const value = await probe();
      if (value) return value;
      await page.waitForTimeout(100);
    }
    throw new Error(message);
  };
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  page.on('response', response => {
    if (response.status() >= 400 && new URL(response.url()).pathname !== '/favicon.ico') {
      errors.push(`HTTP ${response.status()} ${response.url()}`);
    }
  });
  page.on('request', request => {
    if (request.method() === 'POST' && /^\/api\/labeler\/(tasks|export)(\/|$)/.test(new URL(request.url()).pathname)) {
      posts.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() });
    }
  });
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  try {
    await page.request.post(`${BASE}/_fixture/prepare`, { data: {} });
    const before = (await json('/api/labeler/tasks')).tasks.length;
    await page.goto(BASE, { waitUntil: 'load' });
    await page.waitForTimeout(600);
    await checkNoScriptError();
    const openSidebar = page.locator('button[aria-label="打开导航"]:visible').first();
    if (await openSidebar.count()) await openSidebar.click();
    await page.click('#openStudio');
    await page.click('[data-studio-page="labeler"]');
    await page.click('[data-labeler-page-tab="custom"]');
    await pane('config');
    await page.waitForFunction(() => document.querySelector('#labelerModelSelect')?.value === 'retry-model');
    await page.selectOption('#labelerSkillSelect', { label: 'retry-caption' });
    await page.check('#labelerWriteTxtCustom');
    if (mode === 'concurrent') {
      await page.check('#labelerConcurrencyEnabled');
      await page.waitForFunction(() => document.querySelector('#labelerSupervisorModel')?.value === 'retry-model');
      if (await page.locator('#labelerWorkerSlots select').count() < 4) await page.click('#labelerWorkerAdd');
      await page.waitForFunction(() => document.querySelector('#labelerSupervisorModel')?.value === 'retry-model'
        && document.querySelectorAll('#labelerWorkerSlots select').length === 4);
    } else {
      await page.uncheck('#labelerConcurrencyEnabled');
    }
    await checkNoScriptError();
    await page.setInputFiles('#labelerFolderInput', path.join(INPUTS, `${mode}-${viewport.width}`));
    await until(async () => (await page.locator('#labelerPickStatus').textContent()).includes('100 张，可以开始打标'),
      '100 张图片应全部上传成功');
    const created = page.waitForResponse(response => new URL(response.url()).pathname === '/api/labeler/tasks'
      && response.request().method() === 'POST');
    await page.click('#labelerStart');
    const createResponse = await created;
    assert.ok(createResponse.ok());
    const original = (await createResponse.json()).task;
    assert.equal(original.input.config.engine, 'chat');
    assert.equal(Boolean(original.input.config.concurrency_enabled), mode === 'concurrent');
    assert.equal(original.input.config.write_txt, true);
    const taskId = original.id;
    await until(async () => {
      const task = (await json(`/api/labeler/tasks/${taskId}`)).task;
      return task.progress.done === 99 && task.progress.failed === 1 && task.can_retry_failed && task;
    }, '首次应有 99 张成功、1 张失败');
    await until(async () => (await page.locator('#labelerProgressCount').textContent()).trim() === '99 / 100',
      '前端应显示 99 / 100');
    await pane('results');
    await until(async () => await page.locator('.labeler-anno-state[data-state="succeeded"]').count() === 99,
      '原有 99 张成功结果应可见');
    await checkNoScriptError();
    await page.screenshot({ path: path.join(OUTPUT, `${mode}_${viewport.width}_before.png`) });
    await pane('config');
    const retried = page.waitForResponse(response => new URL(response.url()).pathname === `/api/labeler/tasks/${taskId}/retry`);
    await page.click('#labelerRetryModelFailed');
    const retryResponse = await retried;
    assert.ok(retryResponse.ok());
    const retryPayload = await retryResponse.json();
    assert.equal(retryPayload.task.id, taskId, '重试必须沿用原批次 ID');
    await until(async () => (await json('/_fixture/state')).retry_entered, '只有失败图片应进入模型重试');
    assert.equal((await json('/api/labeler/tasks')).tasks.length, before + 1, '重试不应多开批次');
    await pane('results');
    assert.equal(await page.locator('.labeler-anno-state[data-state="succeeded"]').count(), 99,
      '重试期间应保留原来 99 张成功结果');
    await checkNoScriptError();
    await page.request.post(`${BASE}/_fixture/release`, { data: {} });
    await until(async () => {
      const task = (await json(`/api/labeler/tasks/${taskId}`)).task;
      return task.progress.done === 100 && task.progress.failed === 0 && task.status === 'done' && task;
    }, '原批次最终应有 100 张成功');
    await until(async () => (await page.locator('#labelerProgressCount').textContent()).trim() === '100 / 100',
      '前端应显示 100 / 100');
    await until(async () => await page.locator('.labeler-anno-state[data-state="succeeded"]').count() === 100,
      '重试成功后应展示 100 张成功结果');
    const items = (await json(`/api/labeler/tasks/${taskId}/items?limit=100`)).items;
    const state = await json('/_fixture/state');
    assert.equal(items.length, 100);
    for (const item of items) {
      assert.equal(item.state, 'succeeded');
      assert.equal(state.calls[item.item_id].count, item.name === '100.png' ? 2 : 1,
        `${item.name} 的成功结果不得重复生成`);
      assert.equal(item.txt_state, 'auto', `${item.name} 应保留回填的完整 TXT`);
    }
    const txt = await json(`/api/labeler/tasks/${taskId}/txt`);
    assert.equal(txt.files.length, 100, '100 份 TXT 应归于同一批次');
    await page.selectOption('#labelerExportFormat', 'json');
    const exported = page.waitForResponse(response => new URL(response.url()).pathname === '/api/labeler/export');
    const downloaded = page.waitForEvent('download');
    await page.click('#labelerExport');
    const exportPayload = await (await exported).json();
    assert.equal(exportPayload.count, 100, '一次导出必须包含全部 100 条');
    const download = await downloaded;
    const target = path.join(OUTPUT, `${mode}_${viewport.width}_export.json`);
    await download.saveAs(target);
    const rows = JSON.parse(fs.readFileSync(target, 'utf8'));
    assert.equal(rows.length, 100);
    assert.equal(new Set(rows.map(row => row.relative_path)).size, 100);
    assert.ok(rows.some(row => row.name === '100.png'));
    assert.equal(posts.filter(row => row.path === '/api/labeler/tasks').length, 1);
    assert.equal(posts.filter(row => row.path === '/api/labeler/export').length, 1);
    assert.equal(posts.find(row => row.path === '/api/labeler/export').body.task_id, taskId);
    assert.equal((await json('/api/labeler/tasks')).tasks.length, before + 1);
    await pane('queue');
    const card = page.locator(`[data-labeler-task="${taskId}"]`);
    assert.ok((await card.textContent()).includes('100/100 完成'), '原批次卡片应更新到 100/100');
    await checkNoScriptError();
    await page.screenshot({ path: path.join(OUTPUT, `${mode}_${viewport.width}_after.png`) });
    console.log(JSON.stringify({ mode, viewport: viewport.width, taskId, initial: '99/100', final: '100/100',
      retainedSucceeded: 99, generationCalls: 101, txtFiles: txt.files.length, exportCount: rows.length, errors }));
  } catch (error) {
    await page.screenshot({ path: path.join(OUTPUT, `${mode}_${viewport.width}_failure.png`) });
    throw error;
  } finally {
    await page.request.post(`${BASE}/_fixture/release`, { data: {} });
    await page.close();
  }
}

(async () => {
  assert.ok(BASE && OUTPUT && INPUTS, '应通过 _serve_labeler_retry.py 启动隔离实例');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 375, height: 812 }]) {
      for (const mode of ['serial', 'concurrent']) await run(browser, viewport, mode);
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
