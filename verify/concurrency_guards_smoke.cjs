// 源码 UI + 隔离 HTTP/SQLite；工作流响应顺序由浏览器夹具控制。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const fixture = JSON.parse(fs.readFileSync(process.env.NAIBA_GUARDS_FIXTURE, 'utf8'));
const BASE = fixture.base;
const output = process.env.NAIBA_SMOKE_OUTPUT || path.dirname(process.env.NAIBA_GUARDS_FIXTURE);
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const workflow = id => ({ id, name: `工作流 ${id}`, node_count: 1, param_count: 1,
  prompt: { '1': { class_type: 'Textbox', inputs: { text: `提示词 ${id}` } } },
  params: [{ node_id: '1', field_name: 'text', field_type: 'text', control: 'multiline',
    role: 'positive_prompt', node_title: '提示词', default: `提示词 ${id}` }] });

async function run(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  const errors = [], submissions = [], holds = new Map();
  let expectedFailure = '';
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  page.on('response', response => {
    if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')
        && new URL(response.url()).pathname !== expectedFailure) errors.push(`HTTP ${response.status()} ${response.url()}`);
  });
  page.on('dialog', async dialog => { await dialog.accept(); });
  const check = async () => {
    assert.deepEqual(errors, [], '无非预期接口或脚本错误');
    assert.equal(await page.locator('body').evaluate(el => /JS ERROR|PROMISE REJECT/.test(el.innerText)), false);
  };
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  await page.route('**/api/providers/models', route => route.fulfill({ json: { models: [] } }));
  await page.route('**/api/comfyui/**', async route => {
    const p = new URL(route.request().url()).pathname;
    if (p === '/api/comfyui/tasks' && route.request().method() === 'POST') {
      submissions.push(route.request().postDataJSON());
      return route.fulfill({ json: { task: { id: 'fixture-submitted', status: 'completed' } } });
    }
    const id = p.match(/^\/api\/comfyui\/workflows\/([AB])$/)?.[1];
    if (id) {
      const hold = holds.get(id);
      if (hold) { holds.delete(id); hold.started.resolve(); await hold.release.promise; }
      return route.fulfill({ json: { workflow: workflow(id) } });
    }
    const responses = {
      '/api/comfyui/health': { ok: true, url: 'http://127.0.0.1:8188', devices: [] },
      '/api/comfyui/models': { models: [], loras: [] },
      '/api/comfyui/workflows': { workflows: [workflow('A'), workflow('B')] },
      '/api/comfyui/tasks': { tasks: [] }, '/api/comfyui/batches': { batches: [] },
    };
    assert.ok(Object.hasOwn(responses, p), `未预期的造梦机请求 ${p}`);
    return route.fulfill({ json: responses[p] });
  });
  try {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await page.evaluate(async () => { await import(document.querySelector('script[src*="/js/main.js"]').src); });
    await page.evaluate(async () => {
      const studio = await import('/js/22-studio.js'); studio.openStudioView(); studio.setStudioPage('comfyui');
    });
    const pane = name => page.locator(`#comfyuiPanes [data-comfyui-pane="${name}"]`);
    const select = id => page.locator(`[data-workflow-id="${id}"] .comfyui-workflow-title`).click();
    await pane('workflows').click();
    await select('B');
    await page.waitForFunction(() => document.querySelector('.comfyui-param-heading')?.textContent === '工作流 B');
    await check();

    // 已有 B 表单时选 A，等待期间不得仍能提交旧 B。
    await pane('workflows').click();
    const slow = { started: deferred(), release: deferred() }; holds.set('A', slow);
    await select('A'); await slow.started.promise;
    assert.equal(await page.locator('#comfyuiSubmit').isDisabled(), true, '加载期间禁止旧表单提交');
    await select('B');
    await page.waitForFunction(() => document.querySelector('.comfyui-param-heading')?.textContent === '工作流 B');
    slow.release.resolve();
    await page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/workflows/A'));
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.comfyui-param-heading').textContent(), '工作流 B');
    assert.equal(await page.locator('#comfyuiWorkflowList .active').getAttribute('data-workflow-id'), 'B');
    await page.locator('#comfyuiSubmit').click();
    await page.waitForFunction(() => document.querySelector('#comfyuiPanes [data-comfyui-pane="tasks"]')?.getAttribute('aria-pressed') === 'true');
    assert.equal(submissions.length, 1); assert.equal(submissions[0].workflow_id, 'B');
    await check();

    // 真实恢复后的两个同参数任务，在任务面板都应保留并指向各自会话。
    await page.evaluate(async () => { const tasks = await import('/js/06-tasks-plans.js'); await tasks.loadTasks(); });
    await page.evaluate(() => document.querySelector('#openTasks').click());
    await page.locator('#tasksDialog[open]').waitFor();
    for (const job of fixture.jobs) {
      await page.locator(`#taskList [data-task-id="${job.resumed_id}"]`).waitFor({ state: 'visible' });
    }
    const jobsResponse = await page.request.get(`${BASE}/api/tasks?jobs_only=1`);
    const jobs = (await jobsResponse.json()).tasks;
    for (const job of fixture.jobs) assert.equal(jobs.find(row => row.id === job.resumed_id).conversation_id, job.conversation_id);
    await page.locator('#tasksDialog [data-close="tasksDialog"]').click();
    await check();

    // 暂停但仍在执行时点击真实删除按钮，HTTP 拒绝可见；收尾后再次点击成功。
    const thread = fixture.threads[String(width)];
    // 等待页面打开与自动选择收尾，再展开手机侧栏，避免迟到的打开流程将其关闭。
    await page.evaluate(async () => (await import('/js/28-super-agent.js')).openSuperAgentView());
    if (width === 375) await page.locator('#superAgentOpenSidebar').click();
    const remove = page.locator(`[data-thread-delete="${thread.thread_id}"]`);
    await remove.waitFor({ state: 'visible' });
    expectedFailure = `/api/super-agent/threads/${thread.thread_id}/archive`;
    const [deniedResponse] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === expectedFailure), remove.click(),
    ]);
    assert.equal(deniedResponse.status(), 400);
    await page.waitForFunction(() => document.querySelector('#toast')?.textContent.includes('删除失败'));
    assert.equal((await (await page.request.get(`${BASE}/api/super-agent/threads/${thread.thread_id}`)).json()).thread.status, 'paused');
    await check();
    expectedFailure = '';
    const settled = await page.request.post(`${BASE}/_fixture/settle`, { data: { width } });
    assert.equal(settled.status(), 200);
    const [archivedResponse] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname.endsWith(`/${thread.thread_id}/archive`)), remove.click(),
    ]);
    assert.equal(archivedResponse.status(), 200);
    await page.waitForFunction(id => !document.querySelector(`[data-thread-delete="${id}"]`), thread.thread_id);
    assert.equal((await (await page.request.get(`${BASE}/api/super-agent/threads/${thread.thread_id}`)).json()).thread.status, 'archived');
    await check();
    await page.screenshot({ path: path.join(output, `guards-${width}.png`), fullPage: false });
    console.log(`PASS ${width}px: workflow race/loading/submission, separate resumed jobs, archive rejection and completed archive`);
  } catch (error) {
    await page.screenshot({ path: path.join(output, `guards-failed-${width}.png`), fullPage: false });
    console.log('Failure state', JSON.stringify({ errors, toast: await page.locator('#toast').textContent(),
      threads: await page.locator('#superAgentThreadList').innerText() }));
    throw error;
  } finally {
    for (const hold of holds.values()) hold.release.resolve();
    await page.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try { for (const width of [1280, 375]) await run(browser, width); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
