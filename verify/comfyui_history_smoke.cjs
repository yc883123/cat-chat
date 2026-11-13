// 隔离源码服务 + 真实磁盘/API：单次与批次历史分页、虚拟窗口和并发刷新。
// 由 _serve_comfyui_history.py 播种并执行；提交 API 真实落盘，仅外部 runner 由本地完成器替代。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const fixture = JSON.parse(fs.readFileSync(process.env.NAIBA_COMFYUI_HISTORY_FIXTURE, 'utf8'));
const BASE = fixture.base;
const OUTPUT = fixture.output;
const DOM_LIMIT = 120;

async function collectApi(request, kind) {
  const ids = [], sizes = [];
  let cursor = '';
  for (let index = 0; index < 30; index += 1) {
    const query = new URLSearchParams({ page_size: '20' });
    if (kind === 'tasks') query.set('scope', 'single');
    if (cursor) query.set('cursor', cursor);
    const response = await request.get(`${BASE}/api/comfyui/${kind}?${query}`);
    assert.equal(response.status(), 200);
    const body = await response.json();
    assert.equal(body.count, body[kind].length, '分页计数必须与本页记录一致');
    assert.ok(body[kind].length <= 20, '每次只读取有限的一页');
    if (kind === 'tasks') assert.ok(body.tasks.every(row => !row.batch_id), '单次过滤必须在分页前生效');
    ids.push(...body[kind].map(row => row.id));
    sizes.push(body[kind].length);
    if (!body.has_more) break;
    assert.ok(body.next_cursor && body.next_cursor !== cursor, '后续页必须推进游标');
    cursor = body.next_cursor;
  }
  assert.equal(new Set(ids).size, ids.length, '稳定游标不能重复历史记录');
  return { ids, sizes };
}

async function checkViewport(browser, viewport) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const errors = [], responses = [], mediaRequests = [], workflowWrites = [];
  const more = kind => page.locator(`[data-comfyui-history-more="${kind}"]`).first();
  const taskHost = '#comfyuiTaskList';
  const outputHost = '#comfyuiOutputGrid';
  const batchHost = '#comfyuiBatchList';
  const task = id => page.locator(`${taskHost} [data-task-id="${id}"]`);
  const output = id => page.locator(`${outputHost} [data-output-key^="${id}"]`).first();
  const batch = id => page.locator(`${batchHost} [data-batch-open="${id}"]`);
  const apiPath = response => new URL(response.url()).pathname;
  const firstPage = (response, kind) => apiPath(response) === `/api/comfyui/${kind}`
    && response.request().method() === 'GET' && !new URL(response.url()).searchParams.get('cursor');
  await context.addInitScript(() => { localStorage.clear(); localStorage.setItem('naibaOnboardingDismissed', '1'); });
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  page.on('dialog', dialog => dialog.accept());
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname === '/api/file' || /\/api\/comfyui\/tasks\/.*\/files\//.test(url.pathname)) mediaRequests.push(request.url());
    if (/^\/api\/comfyui\/workflows(?:\/|$)/.test(url.pathname) && request.method() !== 'GET') workflowWrites.push(request.url());
  });
  page.on('response', response => {
    const url = new URL(response.url());
    if (response.status() >= 400 && !url.pathname.endsWith('/favicon.ico')) errors.push(`HTTP ${response.status()} ${response.url()}`);
    if (/^\/api\/comfyui\/(tasks|batches)$/.test(url.pathname) && response.request().method() === 'GET') responses.push(url);
  });
  const checkErrors = async () => {
    assert.deepEqual(errors, [], '历史浏览不得产生脚本、媒体或接口错误');
    assert.equal(await page.locator('body').evaluate(node => /JS ERROR|PROMISE REJECT/.test(node.innerText)), false);
  };
  const checkBounds = async () => {
    const counts = await page.evaluate(() => ({
      tasks: document.querySelectorAll('#comfyuiTaskList [data-task-id]').length,
      outputs: document.querySelectorAll('#comfyuiOutputGrid [data-output-key]').length,
      batches: document.querySelectorAll('#comfyuiBatchList [data-batch-open]').length,
    }));
    for (const [name, count] of Object.entries(counts)) assert.ok(count <= DOM_LIMIT, `${name} DOM不能随全部历史无限增长：${count}`);
    await checkErrors();
    return counts;
  };
  const scroll = async (selector, bottom = true) => page.locator(selector).evaluate((host, end) => {
    host.scrollTop = end ? host.scrollHeight : 0;
    host.dispatchEvent(new Event('scroll'));
  }, bottom);
  const reveal = async (selector, target, kind) => {
    for (let attempt = 0; attempt < 35; attempt += 1) {
      if (await target.isVisible()) return;
      await scroll(selector);
      // 滚动本身可能同步触发自动下一页，点击时再次检查，避免等待已隐藏的按钮。
      if (await more(kind).isVisible()) await more(kind).evaluate(button => {
        if (!button.disabled && !button.hidden && button.getBoundingClientRect().height) button.click();
      });
      await page.waitForTimeout(140);
      await checkBounds();
    }
    assert.equal(await target.isVisible(), true, `继续加载/滚动后必须能找到更早历史：${target}`);
  };
  const openResults = async (segment = 'single') => {
    await page.evaluate(async () => {
      const studio = await import('/js/22-studio.js');
      studio.openStudioView(); studio.setStudioPage('comfyui');
    });
    await page.locator('#comfyuiPanes [data-comfyui-pane="tasks"]').click();
    await page.locator(`[data-comfyui-results-seg="${segment}"]`).click();
    await checkErrors();
  };
  const anchor = selector => page.locator(selector).evaluate(host => {
    const top = host.getBoundingClientRect().top;
    const row = Array.from(host.querySelectorAll('[data-task-id], [data-batch-open], [data-output-key]'))
      .find(node => node.getBoundingClientRect().bottom > top + 1);
    return row ? { key: row.dataset.taskId || row.dataset.batchOpen || row.dataset.outputKey,
      offset: row.getBoundingClientRect().top - top, scrollTop: host.scrollTop } : null;
  });
  const sameAnchor = async (selector, before) => {
    const after = await anchor(selector);
    assert.equal(after?.key, before?.key, '后台刷新必须保留当前浏览条目');
    assert.ok(Math.abs(after.offset - before.offset) <= 16, `后台刷新不应跳动当前位置：${JSON.stringify({ before, after })}`);
  };
  try {
    assert.equal((await context.request.post(`${BASE}/_fixture/reset`, { data: {} })).status(), 200);
    const singleApi = await collectApi(context.request, 'tasks');
    const batchApi = await collectApi(context.request, 'batches');
    assert.equal(singleApi.ids.length, fixture.single_count);
    assert.equal(singleApi.ids.at(-1), fixture.oldest_single, '超过旧60条接口上限的单次记录仍可读取');
    assert.equal(batchApi.ids.length, fixture.batch_count);
    assert.equal(batchApi.ids.at(-1), fixture.oldest_batch, '超过旧50批接口上限的批次仍可读取');
    await page.goto(BASE, { waitUntil: 'networkidle' });
    assert.equal(await page.title(), 'Cat Chat');
    await openResults();
    await page.locator(`${taskHost} [data-task-id]`).first().waitFor({ state: 'visible' });
    assert.ok(responses.some(url => url.pathname.endsWith('/tasks') && url.searchParams.get('scope') === 'single'
      && url.searchParams.get('page_size') === '20'), '前端使用单次分页接口');
    await checkBounds();
    await page.waitForTimeout(200);
    assert.ok(!mediaRequests.some(url => decodeURIComponent(url).includes(fixture.oldest_single)), '尚未浏览到的旧媒体不得发起读取');
    const firstOutputScroll = await page.locator(outputHost).evaluate(host => host.scrollTop);
    await reveal(taskHost, task(fixture.oldest_single), 'tasks');
    assert.equal(await page.locator(outputHost).evaluate(host => host.scrollTop), firstOutputScroll, '任务列表与产物网格独立记住滚动位置');
    assert.ok(!mediaRequests.some(url => decodeURIComponent(url).includes(fixture.oldest_single)),
      '历史任务已经加载后，未滚到的旧产物仍不得预载媒体');
    const taskAnchor = await anchor(taskHost);
    const poll = page.waitForResponse(response => firstPage(response, 'tasks'), { timeout: 12000 });
    await context.request.post(`${BASE}/_fixture/update`, { data: { task_id: fixture.newest_single, detail: '轮询后的完成态详情' } });
    await poll;
    await page.waitForTimeout(100);
    await sameAnchor(taskHost, taskAnchor);
    await reveal(outputHost, output(fixture.oldest_single), 'tasks');
    await checkBounds();
    const image = output(fixture.oldest_single).locator('img');
    if (await image.count()) await image.waitFor({ state: 'visible' });
    assert.ok(responses.filter(url => url.pathname.endsWith('/tasks') && url.searchParams.has('cursor')).length >= 4,
      '单次历史通过多页加载到旧记录');

    // 全部历史已加载后再从单次表单提交，成功处理不能清掉中后段却留下旧尾游标。
    await page.locator('#comfyuiPanes [data-comfyui-pane="workflows"]').click();
    await page.locator('#comfyuiWorkflowList [data-workflow-id="cw-history-fixture"] .comfyui-workflow-title').click();
    await page.locator('#comfyuiParamForm [data-comfyui-slot="1|text"]').fill('提交后仍保留已经加载的历史');
    await page.locator('#comfyuiParamForm [data-comfyui-slot="2|seed"]').fill('4242');
    const submission = page.waitForResponse(response => apiPath(response) === '/api/comfyui/tasks'
      && response.request().method() === 'POST');
    await page.locator('#comfyuiSubmit').click();
    const submittedResponse = await submission;
    assert.equal(submittedResponse.status(), 201, '界面提交通过真实服务校验并创建任务');
    const submitted = await submittedResponse.json();
    assert.ok(submitted.task?.id && submitted.task.status === 'completed', '隔离本地runner完成新任务');
    await page.waitForFunction(count => document.querySelector('#studioPageComfyui').dataset.comfyuiPane === 'tasks'
      && document.querySelector('#comfyuiTaskHistoryFooter span').textContent.includes(`${count} 条任务`),
    fixture.single_count + 1, { timeout: 5000 });
    assert.equal(await page.locator('#comfyuiTaskHistoryFooter [data-comfyui-history-more]').isVisible(), false,
      '提交成功后保留全部已加载记录，不重新退回第一页');
    const middleTask = task(singleApi.ids[20]);
    await scroll(taskHost, false);
    for (let attempt = 0; attempt < 90 && !await middleTask.isVisible(); attempt += 1) {
      await page.locator(taskHost).evaluate(host => {
        host.scrollTop += Math.max(80, host.clientHeight * 0.8);
        host.dispatchEvent(new Event('scroll'));
      });
      await page.waitForTimeout(35);
    }
    assert.equal(await middleTask.isVisible(), true, '提交后原第21条历史仍留在已加载范围');
    await reveal(taskHost, task(fixture.oldest_single), 'tasks');
    await checkBounds();

    // 再次生成快照的高级设置只影响本次表单；隐藏项仍保留原覆盖或本次草稿。
    const sourceId = submitted.task.id;
    const sourceBefore = await (await context.request.get(`${BASE}/api/comfyui/tasks/${sourceId}`)).json();
    const snapshotBefore = await (await context.request.get(`${BASE}/api/comfyui/tasks/${sourceId}/rerun-snapshot`)).json();
    const workflowBefore = await (await context.request.get(`${BASE}/api/comfyui/workflows/cw-history-fixture`)).json();
    const writesBefore = workflowWrites.length;
    await scroll(taskHost, false);
    await task(sourceId).locator('[data-comfyui-rerun]').click();
    await page.locator('#comfyuiParamForm .comfyui-rerun-banner').waitFor({ state: 'visible' });
    const field = key => page.locator(`#comfyuiParamForm [data-comfyui-slot="${key}"]`);
    const picker = key => page.locator(`#comfyuiParamsPicker label[data-param-key="${key}"]`);
    const visibleOrder = () => page.locator('#comfyuiParamForm [data-comfyui-slot]')
      .evaluateAll(rows => rows.map(row => row.dataset.comfyuiSlot));
    const storedDisplay = () => page.evaluate(() => Object.fromEntries(Object.entries(localStorage)
      .filter(([key]) => key.startsWith('comfyui.'))));
    const storageBefore = await storedDisplay();
    assert.equal(await field('2|seed').inputValue(), '4242', '固定seed显示原任务显式覆盖，而不是原workflow的1111');
    assert.equal(await field('2|cfg').count(), 0, '额外数字参数初始不显示');
    assert.equal(await field('2|enabled').count(), 0, '额外开关参数初始不显示');
    await page.locator('#comfyuiParamForm [data-comfyui-edit-params]').click();
    await page.locator('#comfyuiParamsDialog').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#comfyuiParamsSave').textContent(), '应用');
    assert.ok((await page.locator('#comfyuiParamsHint').textContent()).includes('只用于本次再次生成'));
    await picker('2|cfg').locator('input').check();
    await picker('2|enabled').locator('input').check();
    await picker('1|text').locator('input').uncheck();
    for (let index = 0; index < 3; index += 1) await picker('2|enabled').locator('[data-move="up"]').click();
    await page.locator('#comfyuiParamsSave').click();
    await page.locator('#comfyuiParamsDialog').waitFor({ state: 'hidden' });
    const editedOrder = await visibleOrder();
    assert.equal(editedOrder[0], '2|enabled', '上移操作影响本次表单顺序');
    assert.equal(await field('1|text').count(), 0, '原任务提示词覆盖可以隐藏');
    assert.equal(await field('2|cfg').inputValue(), '6.5');
    assert.equal(await field('2|enabled').inputValue(), 'true');
    await field('2|cfg').fill('8.25');
    await field('2|enabled').selectOption('false');
    await field('2|seed').fill('123');
    await page.locator('#comfyuiParamForm [data-comfyui-reset-params]').click();
    assert.deepEqual(await visibleOrder(), editedOrder, '默认恢复值时保留本次显示项与顺序');
    assert.equal(await field('2|cfg').inputValue(), '6.5', '默认恢复快照的数字值');
    assert.equal(await field('2|enabled').inputValue(), 'true', '默认恢复快照的开关值');
    assert.equal(await field('2|seed').inputValue(), '4242', '默认恢复原任务固定seed');
    await page.screenshot({ path: path.join(OUTPUT, `history_snapshot_${viewport.width}.png`) });
    await field('2|cfg').fill('9.75');
    await field('2|enabled').selectOption('false');
    await field('2|seed').fill('');
    await page.locator('#comfyuiParamForm [data-comfyui-edit-params]').click();
    await picker('2|cfg').locator('input').uncheck();
    await picker('2|enabled').locator('input').uncheck();
    await page.locator('#comfyuiParamsSave').click();
    assert.deepEqual(await visibleOrder(), ['2|seed'], '隐藏额外参数后仅保留seed控件');
    assert.equal(await field('2|seed').inputValue(), '', '高级设置重绘后清空seed仍保持随机');
    assert.equal(workflowWrites.length, writesBefore, '快照应用与默认不得调用workflow写接口');
    assert.deepEqual(await storedDisplay(), storageBefore, '快照显示项和顺序不得持久化到storage');
    const rerunSubmission = page.waitForResponse(response => apiPath(response) === `/api/comfyui/tasks/${sourceId}/rerun`
      && response.request().method() === 'POST');
    await page.locator('#comfyuiSubmit').click();
    const rerunResponse = await rerunSubmission;
    assert.equal(rerunResponse.status(), 201, '快照高级参数通过真实再次生成API校验');
    const rerunBody = rerunResponse.request().postDataJSON();
    const overrides = Object.fromEntries(rerunBody.overrides.map(row => [`${row.node_id}|${row.field_name}`, row.value]));
    assert.equal(overrides['1|text'], '提交后仍保留已经加载的历史', '隐藏的原任务覆盖仍随新任务提交');
    assert.equal(overrides['2|cfg'], '9.75', '隐藏的本次数字草稿仍随新任务提交');
    assert.equal(overrides['2|enabled'], false, '隐藏的本次开关草稿保持布尔类型');
    assert.equal(Object.hasOwn(overrides, '2|seed'), false, '清空固定seed后不提交覆盖，恢复随机');
    const rerunTask = (await rerunResponse.json()).task;
    assert.equal(rerunTask.status, 'completed');
    assert.notEqual(rerunTask.id, sourceId, '再次生成创建独立任务');
    assert.deepEqual(await (await context.request.get(`${BASE}/api/comfyui/tasks/${sourceId}`)).json(), sourceBefore,
      '再次生成高级参数不改源任务');
    assert.deepEqual(await (await context.request.get(`${BASE}/api/comfyui/tasks/${sourceId}/rerun-snapshot`)).json(), snapshotBefore,
      '原任务冻结快照保持不变');
    assert.deepEqual(await (await context.request.get(`${BASE}/api/comfyui/workflows/cw-history-fixture`)).json(), workflowBefore,
      '再次生成高级参数不改原workflow');
    assert.equal(workflowWrites.length, writesBefore);
    await page.waitForFunction(() => document.querySelector('#studioPageComfyui').dataset.comfyuiPane === 'tasks');
    await reveal(taskHost, task(fixture.oldest_single), 'tasks');
    await checkBounds();

    const beforeAdd = await anchor(taskHost);
    const added = await (await context.request.post(`${BASE}/_fixture/add`, { data: { kind: 'single' } })).json();
    await page.waitForResponse(response => firstPage(response, 'tasks'), { timeout: 12000 });
    await page.waitForTimeout(100);
    await sameAnchor(taskHost, beforeAdd);
    await scroll(taskHost, false);
    await task(added.id).waitFor({ state: 'visible' });
    await task(added.id).locator('[data-comfyui-delete-task]').click();
    await task(added.id).waitFor({ state: 'detached' });
    assert.equal((await context.request.get(`${BASE}/api/comfyui/tasks/${added.id}`)).status(), 404,
      'UI删除走真实后端并移除磁盘任务');
    await reveal(taskHost, task(fixture.oldest_single), 'tasks');
    await checkBounds();

    await page.locator('[data-comfyui-results-seg="batch"]').click();
    await page.locator(`${batchHost} [data-batch-open]`).first().waitFor({ state: 'visible' });
    await reveal(batchHost, batch(fixture.oldest_batch), 'batches');
    const batchAnchor = await anchor(batchHost);
    const addedBatch = await (await context.request.post(`${BASE}/_fixture/add`, { data: { kind: 'batch' } })).json();
    await page.waitForResponse(response => firstPage(response, 'batches'), { timeout: 12000 });
    await page.waitForTimeout(100);
    await sameAnchor(batchHost, batchAnchor);
    await scroll(batchHost, false);
    await batch(addedBatch.id).waitFor({ state: 'visible' });
    await batch(addedBatch.id).click();
    await page.locator('#comfyuiBatchDetail [data-batch-action="delete-batch"]').waitFor({ state: 'visible' });
    await page.locator('#comfyuiBatchDetail [data-batch-action="delete-batch"]').click();
    await page.locator(`${batchHost} [data-batch-open]`).first().waitFor({ state: 'visible' });
    assert.equal(await batch(addedBatch.id).count(), 0);
    await reveal(batchHost, batch(fixture.oldest_batch), 'batches');
    await checkBounds();

    // 取到真实旧响应后暂停交付；其间删除旧页任务，新一代刷新不得被旧响应复活。
    await context.request.post(`${BASE}/_fixture/reset`, { data: {} });
    await page.reload({ waitUntil: 'networkidle' });
    await openResults();
    await task(fixture.newest_single).waitFor({ state: 'visible' });
    let hold = true, release = null, readyResolve;
    const ready = new Promise(resolve => { readyResolve = resolve; });
    await page.route('**/api/comfyui/tasks?*', async route => {
      const url = new URL(route.request().url());
      if (!hold || route.request().method() !== 'GET' || url.searchParams.get('cursor')) return route.continue();
      hold = false;
      const response = await route.fetch();
      const body = await response.body();
      await new Promise(resolve => {
        release = async () => { await route.fulfill({ response, body }); resolve(); };
        readyResolve();
      });
    });
    await ready;
    await task(fixture.newest_single).locator('[data-comfyui-delete-task]').click();
    await task(fixture.newest_single).waitFor({ state: 'detached' });
    await release();
    await page.waitForTimeout(200);
    assert.equal(await task(fixture.newest_single).count(), 0, '迟到的旧第一页不能复活已删除任务');
    await reveal(taskHost, task(fixture.oldest_single), 'tasks');
    await scroll(taskHost, false);
    assert.equal(await task(fixture.newest_single).count(), 0, '继续加载更早分页后删除状态仍生效');
    await checkBounds();

    // 后续页在途时删除已加载记录：旧分页不能恢复失效状态或破坏下一游标。
    await context.request.post(`${BASE}/_fixture/reset`, { data: {} });
    await page.reload({ waitUntil: 'networkidle' });
    await openResults();
    await task(fixture.newest_single).waitFor({ state: 'visible' });
    let holdCursor = true, releaseCursor = null, cursorReadyResolve;
    const cursorReady = new Promise(resolve => { cursorReadyResolve = resolve; });
    await page.route('**/api/comfyui/tasks?*', async route => {
      const url = new URL(route.request().url());
      if (!holdCursor || !url.searchParams.get('cursor')) return route.continue();
      holdCursor = false;
      const response = await route.fetch();
      const body = await response.body();
      await new Promise(resolve => {
        releaseCursor = async () => { await route.fulfill({ response, body }); resolve(); };
        cursorReadyResolve();
      });
    });
    await more('tasks').click();
    await cursorReady;
    await task(fixture.newest_single).locator('[data-comfyui-delete-task]').click();
    await task(fixture.newest_single).waitFor({ state: 'detached' });
    await releaseCursor();
    await page.waitForTimeout(150);
    await reveal(taskHost, task(fixture.oldest_single), 'tasks');
    await scroll(taskHost, false);
    assert.equal(await task(fixture.newest_single).count(), 0, '迟到分页及后续加载不能复活已删任务');
    await checkBounds();

    // 首次读到空历史后新增多页：第一次非空刷新必须恢复尾游标和“还有更多”。
    await context.request.post(`${BASE}/_fixture/reset`, { data: {} });
    let emptyHistory = true;
    await page.route(/\/api\/comfyui\/(tasks|batches)\?/, route => {
      const url = new URL(route.request().url());
      if (!emptyHistory || route.request().method() !== 'GET' || !url.searchParams.has('page_size')) return route.continue();
      const kind = url.pathname.endsWith('/tasks') ? 'tasks' : 'batches';
      return route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ [kind]: [], count: 0, next_cursor: '', has_more: false }) });
    });
    await page.reload({ waitUntil: 'networkidle' });
    await openResults();
    await page.waitForFunction(() => document.querySelector('#comfyuiTaskHistoryFooter span').textContent.includes('已加载全部 0')
      && document.querySelector('#comfyuiBatchHistoryFooter span').textContent.includes('已加载全部 0'));
    emptyHistory = false;
    await page.waitForResponse(response => firstPage(response, 'tasks'), { timeout: 12000 });
    await page.locator(`${taskHost} [data-task-id]`).first().waitFor({ state: 'visible' });
    await reveal(taskHost, task(fixture.oldest_single), 'tasks');
    assert.ok((await page.locator('#comfyuiTaskHistoryFooter span').textContent()).includes(`${fixture.single_count} 条任务`),
      '从空列表新增多页后仍能完整加载单次历史');
    await page.locator('[data-comfyui-results-seg="batch"]').click();
    await reveal(batchHost, batch(fixture.oldest_batch), 'batches');
    assert.ok((await page.locator('#comfyuiBatchHistoryFooter span').textContent()).includes(`${fixture.batch_count} 个批次`),
      '从空列表新增多页后仍能完整加载批次历史');
    await checkBounds();
    await page.screenshot({ path: path.join(OUTPUT, `history_${viewport.width}.png`) });
    console.log(`通过 ${viewport.width}×${viewport.height}：96单次/72批真实分页、虚拟窗口、独立滚动、媒体懒读、提交保留历史、快照高级参数、空列表后多页新增、轮询位置保留与删除并发`);
  } catch (error) {
    console.error('历史验收错误：', errors);
    await page.screenshot({ path: path.join(OUTPUT, `history_failed_${viewport.width}.png`) });
    throw error;
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const viewport of [{ width: 1280, height: 800 }, { width: 375, height: 812 }]) await checkViewport(browser, viewport);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
