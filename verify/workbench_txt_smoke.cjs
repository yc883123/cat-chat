// 固定本机源码与隔离数据；模型边界替身，TXT执行、存储和消费走生产链路。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const fixture = JSON.parse(fs.readFileSync(process.env.NAIBA_WORKBENCH_TXT_FIXTURE, 'utf8'));
const BASE = fixture.base, OUTPUT = fixture.runtime;

async function json(context, endpoint, method = 'get', body) {
  const response = await context.request[method](BASE + endpoint, body === undefined ? {} : { data: body });
  assert.ok(response.ok(), `${method} ${endpoint}: ${response.status()} ${await response.text()}`);
  return response.json();
}
async function taskDone(context, id, status = 'done') {
  for (let attempt = 0; attempt < 160; attempt++) {
    const { task } = await json(context, `/api/studio/tasks/${id}`);
    if (['done', 'failed', 'cancelled', 'interrupted'].includes(task.status)) {
      assert.equal(task.status, status, JSON.stringify(task)); return task;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw Error(`任务未结束：${id}`);
}
async function enter(page, domain, book) {
  await page.evaluate(async ({ domain, bookId }) => {
    const studio = await import('/js/22-studio.js'); studio.openStudioView(); studio.setStudioPage(domain);
    window.dispatchEvent(new CustomEvent('naiba-open-domain', { detail: { domain, bookId } }));
  }, { domain, bookId: book.id });
  await page.waitForFunction(({ domain, title }) => document.querySelector(`#${domain}BookTitle`)?.textContent.includes(title),
    { domain, title: book.title });
}
async function pane(page, domain, name) {
  const button = page.locator(`#${domain}Panes [data-${domain}-pane="${name}"]`);
  if (await button.isVisible()) await button.click();
}
async function writerCancellation(page, context, source, checkNoScriptError) {
  const width = source.width;
  const report = [];
  const snapshot = () => json(context, '/_fixture/writer-cancel');
  const control = body => json(context, '/_fixture/writer-cancel', 'post', body);
  const url = book => `/api/writer/books/${book.id}`;
  const fresh = async book => (await json(context, `${url(book)}/pipeline`)).pipeline;
  const start = async (book, settings = {}) => (await json(context, `${url(book)}/pipelines`, 'post', {
    kind: 'setup_step', step: 'world', model_key: 'online:txt-fixture', model_name: 'txt-fixture', ...settings,
  })).pipeline;
  const wait = async (label, read, condition) => {
    let latest;
    for (let attempt = 0; attempt < 200; attempt++) {
      latest = await read();
      if (condition(latest)) return latest;
      await page.waitForTimeout(100);
    }
    throw Error(`${label}: ${JSON.stringify(latest)}`);
  };
  const showProgress = async () => {
    await pane(page, 'writer', 'notes');
    if (!(await page.locator('#writerPipelineCancel').isVisible()) && await page.locator('#writerAsideBtn').isVisible()) {
      await page.locator('#writerAsideBtn').click();
    }
  };
  const clickCancel = async (book, expectedStatus = 200) => {
    const waiting = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === `${url(book)}/pipeline/cancel`);
    await page.locator('#writerPipelineCancel').click();
    const response = await waiting;
    assert.equal(response.status(), expectedStatus, await response.text());
    const request = response.request().postDataJSON();
    assert.ok(request.pipeline_id && Number.isInteger(request.attempt), '取消携带流水线身份');
    await checkNoScriptError();
    return { request, payload: await response.json() };
  };
  const cancelCurrent = async book => {
    const current = await fresh(book);
    return json(context, `${url(book)}/pipeline/cancel`, 'post', { pipeline_id: current.id, attempt: current.attempt });
  };
  const controls = async locked => {
    await page.locator('[data-writer-stage="setup"]').click();
    await pane(page, 'writer', 'book');
    assert.ok(await page.locator('[data-writer-setup-version]').count() >= 2);
    await page.waitForFunction(expected => [...document.querySelectorAll('[data-writer-setup-version]')]
      .every(button => button.disabled === expected), locked);
    await pane(page, 'writer', 'editor');
    await page.locator('[data-writer-stage="outline"]').click();
    assert.equal(await page.locator('#writerOutlineVersion').isDisabled(), locked);
    await page.locator('[data-writer-stage="chapter"]').click();
    await pane(page, 'writer', 'book');
    assert.equal(await page.locator('#writerChapterOutlineVersion').isDisabled(), locked);
    await pane(page, 'writer', 'editor');
    await page.locator('[data-writer-stage="draft"]').click();
    await pane(page, 'writer', 'book');
    await page.locator('[data-writer-chapter="1"]').click();
    await pane(page, 'writer', 'editor');
    assert.equal(await page.locator('#writerDraftVersion').isDisabled(), locked);
    await checkNoScriptError();
  };
  let dialogAccept = true;
  const dialogs = [];
  page.removeAllListeners('dialog');
  page.on('dialog', dialog => {
    dialogs.push(dialog.message());
    return dialogAccept ? dialog.accept() : dialog.dismiss();
  });

  // A real completed step precedes cooperative cancellation; user drafts survive the terminal refresh.
  const book = source.writer;
  const before = await json(context, url(book));
  const chapterBefore = await json(context, `${url(book)}/chapters/1`);
  await start(book, { kind: 'setup_all', char_count: 1, side_count: 0 });
  const blocked = await wait('协作模型进入书名步', snapshot,
    state => state.events.some(event => event.book_id === book.id && event.event === 'model_entered'));
  const blockingTask = blocked.events.find(event => event.book_id === book.id && event.event === 'model_entered');
  const committed = await json(context, url(book));
  assert.equal(committed.setup.genre.cat, '都市悬疑');
  assert.equal(committed.book.setup_versions.length, before.book.setup_versions.length + 1);
  assert.equal((await fresh(book)).completed_task_ids.length, 1);
  await enter(page, 'writer', book);
  await controls(true);
  const bodyDraft = `取消后仍须保留的未保存正文 ${width}`;
  await page.locator('#writerDraftContent').fill(bodyDraft);
  await page.locator('[data-writer-stage="setup"]').click();
  const ideaDraft = `取消前手写创意 ${width}`;
  await page.locator('#writerIdea').fill(ideaDraft);
  await page.locator('[data-writer-stage="outline"]').click();
  const outlineDraft = `取消前手写第一部大纲 ${width}`;
  await page.locator('#writerOutlineText').fill(outlineDraft);
  await showProgress();
  const requestedAt = Date.now();
  await clickCancel(book);
  const cancelled = await wait('协作取消终态', () => fresh(book), pipeline => pipeline.status === 'cancelled');
  await page.waitForFunction(() => document.querySelector('#writerPipelineStatus').textContent === '已取消');
  assert.equal(cancelled.step_index, 1);
  assert.equal(cancelled.completed_task_ids.length, 1);
  assert.equal((await json(context, `/api/studio/tasks/${blockingTask.task_id}`)).task.status, 'cancelled');
  await wait('终态保存反馈', () => page.locator('#writerSaveState').innerText(), text => text.includes('取消'));
  await controls(false);
  assert.equal(await page.locator('#writerDraftContent').inputValue(), bodyDraft);
  await page.locator('[data-writer-stage="setup"]').click();
  assert.equal(await page.locator('#writerIdea').inputValue(), ideaDraft);
  await page.locator('[data-writer-stage="outline"]').click();
  assert.equal(await page.locator('#writerOutlineText').inputValue(), outlineDraft);
  const preserved = await json(context, url(book));
  assert.deepEqual(preserved.book.setup_versions, committed.book.setup_versions);
  assert.deepEqual(await json(context, `${url(book)}/chapters/1`), chapterBefore);
  await page.screenshot({ path: path.join(OUTPUT, `writer-cancel-drafts-${width}.png`) });
  report.push({ scenario: 'cooperative', passed: true, cancellation_ms: Date.now() - requestedAt });

  // Retry after a content-version switch first rejects without enqueueing, then asks to rebase.
  await page.locator('[data-writer-stage="setup"]').click();
  await pane(page, 'writer', 'book');
  const switched = page.waitForResponse(response => new URL(response.url()).pathname === `${url(book)}/versions/select`);
  await page.locator(`[data-writer-setup-version="${before.book.setup_versions[0].id}"]`).click();
  assert.equal((await switched).status(), 200);
  await showProgress();
  const beforeRetry = (await snapshot()).submissions.length;
  const retries = [];
  const observeRetry = request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === `${url(book)}/pipeline/retry`) retries.push(request.postDataJSON());
  };
  page.on('request', observeRetry);
  const dialogCount = dialogs.length;
  dialogAccept = false;
  await page.locator('#writerPipelineRetry').click();
  await wait('换基确认被拒绝', async () => dialogs.length, count => count === dialogCount + 1);
  await page.waitForTimeout(250);
  assert.equal((await snapshot()).submissions.length, beforeRetry, '确认拒绝不提交新模型任务');
  assert.equal((await fresh(book)).attempt, cancelled.attempt);
  assert.equal((await fresh(book)).status, 'cancelled');
  assert.equal(retries.length, 1);
  assert.ok(!retries[0].rebase);
  dialogAccept = true;
  await page.locator('#writerPipelineRetry').click();
  const finished = await wait('确认换基后完成', () => fresh(book), pipeline => pipeline.status === 'done');
  assert.equal(finished.attempt, cancelled.attempt + 1);
  assert.equal(retries.filter(request => request.rebase === true).length, 1, '确认换基仅提交一次');
  assert.ok(retries.every(request => request.pipeline_id === cancelled.id && request.attempt === cancelled.attempt));
  const retryTitle = (await snapshot()).submissions.find(task => task.book_id === book.id && task.step === 'title' && task.attempt === 2);
  assert.equal(retryTitle.source_version_ids.setup, before.book.setup_versions[0].id, '重试冻结用户确认的当前设定版本');
  page.off('request', observeRetry);
  await page.waitForFunction(() => document.querySelector('#writerPipelineStatus').textContent === '已完成');
  await page.screenshot({ path: path.join(OUTPUT, `writer-cancel-rebase-${width}.png`) });
  await checkNoScriptError();
  report.push({ scenario: 'retry_rebase', passed: true, requests: retries });

  // An overdue submit without a task handle releases the book; its late handle remains cancelled.
  const submittingBook = source.cancel_books.submit;
  const initialSubmit = await json(context, url(submittingBook));
  const starting = start(submittingBook).then(pipeline => ({ pipeline }), error => ({ error: error.message }));
  await wait('在途提交已进入', snapshot, state => state.entered_submissions.includes(submittingBook.id));
  await enter(page, 'writer', submittingBook);
  await showProgress();
  const stopping = (await clickCancel(submittingBook)).payload.pipeline;
  assert.equal(stopping.status, 'stopping');
  assert.equal(stopping.current_task_id, '');
  await control({ action: 'age', book_id: submittingBook.id });
  await wait('无句柄停止超时解锁', () => fresh(submittingBook), pipeline => pipeline.status === 'cancelled');
  await page.waitForFunction(() => !document.querySelector('[data-writer-setup-version]').disabled);
  const replacement = await start(submittingBook);
  assert.notEqual(replacement.id, stopping.id);
  await control({ action: 'release', book_id: submittingBook.id });
  const returned = await starting;
  if (returned.error) throw Error(returned.error);
  const late = await wait('迟到任务句柄登记', snapshot,
    state => state.submissions.some(task => task.book_id === submittingBook.id && task.pipeline_id === stopping.id));
  const oldTask = late.submissions.find(task => task.book_id === submittingBook.id && task.pipeline_id === stopping.id);
  await taskDone(context, oldTask.task_id, 'cancelled');
  assert.equal((await fresh(submittingBook)).id, replacement.id);
  assert.equal((await fresh(submittingBook)).status, 'running');
  assert.deepEqual((await json(context, url(submittingBook))).book.setup_versions, initialSubmit.book.setup_versions);
  await page.screenshot({ path: path.join(OUTPUT, `writer-cancel-late-submit-${width}.png`) });
  await cancelCurrent(submittingBook);
  await checkNoScriptError();
  report.push({ scenario: 'overdue_submit', passed: true, late_task_id: oldTask.task_id });

  // Freeze only this browser's polling while another client replaces its queued generation.
  const staleBook = source.cancel_books.stale;
  const oldPipeline = await start(staleBook);
  await enter(page, 'writer', staleBook);
  await showProgress();
  await page.waitForFunction(() => document.querySelector('#writerPipelineStatus').textContent === '进行中');
  let releasePoll;
  const pollingHeld = new Promise(resolve => { releasePoll = resolve; });
  const pollingRoute = async route => { await pollingHeld; await route.continue().catch(() => {}); };
  await page.route(`**${url(staleBook)}/pipeline`, pollingRoute);
  try {
    await cancelCurrent(staleBook);
    const newer = await start(staleBook);
    const staleCancel = await clickCancel(staleBook, 409);
    assert.equal(staleCancel.request.pipeline_id, oldPipeline.id);
    assert.equal(staleCancel.request.attempt, oldPipeline.attempt);
    assert.equal((await fresh(staleBook)).id, newer.id);
    assert.equal((await fresh(staleBook)).status, 'running');
    assert.equal((await json(context, `/api/studio/tasks/${newer.current_task_id}`)).task.status, 'queued');
    releasePoll();
    await page.unroute(`**${url(staleBook)}/pipeline`, pollingRoute);
    await page.waitForTimeout(1000);
    const currentCancel = await clickCancel(staleBook);
    assert.equal(currentCancel.request.pipeline_id, newer.id, '拒绝旧请求后页面刷新到真实新流水线');
    assert.equal(currentCancel.payload.pipeline.status, 'cancelled');
    await page.screenshot({ path: path.join(OUTPUT, `writer-cancel-stale-${width}.png`) });
    await checkNoScriptError();
  } finally {
    releasePoll();
    await page.unroute(`**${url(staleBook)}/pipeline`, pollingRoute);
  }
  report.push({ scenario: 'stale_cancel', passed: true });

  // A queued task cancels immediately through the real runner; terminal refresh updates feedback.
  const queuedBook = source.cancel_books.queued;
  await enter(page, 'writer', queuedBook);
  await page.locator('[data-writer-stage="setup"]').click();
  await pane(page, 'writer', 'editor');
  const submitting = page.waitForResponse(response => new URL(response.url()).pathname === `${url(queuedBook)}/pipelines`);
  await page.locator('#writerSetupGenerate').click();
  assert.equal((await submitting).status(), 201);
  const queued = await fresh(queuedBook);
  assert.equal((await json(context, `/api/studio/tasks/${queued.current_task_id}`)).task.status, 'queued');
  const queuedBefore = await json(context, url(queuedBook));
  const queuedChapter = await json(context, `${url(queuedBook)}/chapters/1`);
  const queuedIdea = `排队期间未保存创意 ${width}`;
  await page.locator('#writerIdea').fill(queuedIdea);
  await control({ action: 'schedule', task_id: queued.current_task_id });
  await taskDone(context, queued.current_task_id);
  const secondQueued = await wait('第一步保存后第二步排队', () => fresh(queuedBook),
    pipeline => pipeline.step_index === 1 && pipeline.current_task_id !== queued.current_task_id);
  assert.equal((await json(context, `/api/studio/tasks/${secondQueued.current_task_id}`)).task.status, 'queued');
  const committedQueued = await json(context, url(queuedBook));
  assert.equal(committedQueued.setup.genre.cat, '都市悬疑');
  assert.equal(committedQueued.book.setup_versions.length, queuedBefore.book.setup_versions.length + 1);
  assert.equal(await page.locator('[data-writer-setup-version]').count(), queuedBefore.book.setup_versions.length,
    '取消前页面尚未读取后台新保存版本，确保验收终态刷新');
  await showProgress();
  assert.equal(await page.locator('#writerSaveState').innerText(), '已提交');
  const immediate = (await clickCancel(queuedBook)).payload.pipeline;
  assert.equal(immediate.status, 'cancelled');
  await wait('立即取消更新保存反馈', () => page.locator('#writerSaveState').innerText(), text => text.includes('取消'));
  await page.waitForFunction(() => document.querySelector('#writerPipelineStatus').textContent === '已取消');
  await pane(page, 'writer', 'editor');
  assert.equal(await page.locator('#writerSetupGenerate').isEnabled(), true);
  assert.equal(await page.locator('#writerIdea').inputValue(), queuedIdea);
  await wait('立即取消后新保存版本显示在页面', () => page.locator('[data-writer-setup-version]').count(),
    count => count === committedQueued.book.setup_versions.length);
  assert.equal(await page.locator(`[data-writer-setup-version="${committedQueued.book.current_setup_version_id}"]`).count(), 1);
  assert.deepEqual((await json(context, url(queuedBook))).book.setup_versions, committedQueued.book.setup_versions);
  assert.deepEqual(await json(context, `${url(queuedBook)}/chapters/1`), queuedChapter);
  await pane(page, 'writer', 'book');
  await page.screenshot({ path: path.join(OUTPUT, `writer-cancel-queued-${width}.png`) });
  await checkNoScriptError();
  report.push({ scenario: 'queued_cancel', passed: true, committed_step: 'genre',
    stored_setup_versions: committedQueued.book.setup_versions.length,
    rendered_setup_versions: await page.locator('[data-writer-setup-version]').count() });
  fs.writeFileSync(path.join(OUTPUT, `writer-cancel-report-${width}.json`), JSON.stringify(report, null, 2));
  console.log(`Writer取消 ${width}px：已保存步骤与草稿保留、版本变化确认换基、无句柄超时与迟到提交、旧页面取消、排队立即取消通过`);
}
async function writerVersionLock(page, context, source, checkNoScriptError) {
  const book = source.writer, width = source.width;
  const bookUrl = `/api/writer/books/${book.id}`;
  const before = await json(context, bookUrl);
  const chapterBefore = await json(context, `${bookUrl}/chapters/1`);
  assert.equal(before.book.setup_versions.length, 2);
  assert.equal(before.book.outline_versions.length, 2);
  assert.equal(chapterBefore.versions.length, 2);
  const started = (await json(context, `${bookUrl}/pipelines`, 'post', {
    kind: 'setup_step', step: 'world', model_key: 'online:txt-fixture', model_name: 'txt-fixture',
    extra_note: `TXT_WORLD_${width}`,
  })).pipeline;
  for (let attempt = 0; attempt < 160; attempt++) {
    if ((await json(context, '/_fixture/version-lock')).entered.includes(width)) break;
    await page.waitForTimeout(100);
  }
  assert.ok((await json(context, '/_fixture/version-lock')).entered.includes(width), '真实流水线已进入等待释放的模型调用');
  await enter(page, 'writer', book);

  const versionControls = async locked => {
    await page.locator('[data-writer-stage="setup"]').click();
    await pane(page, 'writer', 'book');
    const setupRows = page.locator('[data-writer-setup-version]');
    assert.ok(await setupRows.count() >= 2);
    await page.waitForFunction(expected => [...document.querySelectorAll('[data-writer-setup-version]')]
      .every(button => button.disabled === expected), locked);
    for (const control of await setupRows.all()) assert.equal(await control.isDisabled(), locked, '设定版本按钮锁定状态');
    await pane(page, 'writer', 'editor');
    await page.locator('[data-writer-stage="outline"]').click();
    const outline = page.locator('#writerOutlineVersion');
    await outline.waitFor({ state: 'visible' });
    assert.equal(await outline.evaluate(element => element.tagName), 'SELECT');
    assert.equal(await outline.isDisabled(), locked, '大纲页原生版本选择锁定状态');
    await page.locator('[data-writer-stage="chapter"]').click();
    await pane(page, 'writer', 'book');
    const chapterOutline = page.locator('#writerChapterOutlineVersion');
    await chapterOutline.waitFor({ state: 'visible' });
    assert.equal(await chapterOutline.evaluate(element => element.tagName), 'SELECT');
    assert.equal(await chapterOutline.isDisabled(), locked, '章节页原生大纲版本选择锁定状态');
    await pane(page, 'writer', 'editor');
    await page.locator('[data-writer-stage="draft"]').click();
    await pane(page, 'writer', 'book');
    await page.locator('[data-writer-chapter="1"]').click();
    await pane(page, 'writer', 'editor');
    const draft = page.locator('#writerDraftVersion');
    await draft.waitFor({ state: 'visible' });
    assert.equal(await draft.isDisabled(), locked, '正文版本选择锁定状态');
    await draft.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(OUTPUT, `writer-version-${locked ? 'locked' : 'ready'}-${width}.png`) });
  };
  const managementControls = async locked => {
    await page.locator('#writerMoreBtn').click();
    await page.locator('#writerManageBtn').click();
    for (const tab of ['setup', 'outline', 'chapter']) {
      await page.locator(`[data-writer-manage-tab="${tab}"]`).click();
      if (tab === 'chapter') {
        await page.locator('[data-writer-manage-open="chapter:1"]').click();
        await page.locator('[data-writer-manage-versions="1"]').waitFor();
      }
      const useButtons = page.locator('[data-writer-manage-use]');
      assert.ok(await useButtons.count() >= 1, `${tab}存在旧版本切换入口`);
      for (const button of await useButtons.all()) assert.equal(await button.isDisabled(), locked, `${tab}管理记录设为当前锁定状态`);
      const read = tab === 'chapter'
        ? page.locator(`[data-writer-manage-version-open="${chapterBefore.current_version_id}"]`)
        : page.locator(`[data-writer-manage-open="version:${tab === 'setup'
          ? before.book.current_setup_version_id : before.book.current_outline_version_id}"]`);
      assert.equal(await read.isEnabled(), true, `${tab}管理记录仍可读取`);
      await read.click();
      await page.locator('#writerManagePreview').waitFor({ state: 'visible' });
      assert.ok((await page.locator('#writerManagePreviewBody').innerText()).trim(), `${tab}阅读动作展示内容`);
    }
    await page.screenshot({ path: path.join(OUTPUT, `writer-version-management-${locked ? 'locked' : 'ready'}-${width}.png`) });
    await page.locator('#writerManageClose').click();
  };
  try {
    await versionControls(true);
    await managementControls(true);
    for (const command of [
      { kind: 'setup', version_id: before.book.setup_versions[0].id, revision: before.book.revision },
      { kind: 'outline', version_id: before.book.outline_versions[0].id, revision: before.book.revision },
      { kind: 'chapter', chapter_no: 1, version_id: chapterBefore.versions[0].id, revision: chapterBefore.revision },
    ]) {
      const rejected = await context.request.post(BASE + `${bookUrl}/versions/select`, { data: command });
      assert.equal(rejected.status(), 409, `${command.kind}直接请求也拒绝切换`);
      assert.equal((await rejected.json()).error, '当前生成尚未结束，请结束后再切换版本');
    }
    const unchanged = await json(context, bookUrl);
    const chapterUnchanged = await json(context, `${bookUrl}/chapters/1`);
    assert.equal(unchanged.book.current_setup_version_id, before.book.current_setup_version_id);
    assert.equal(unchanged.book.current_outline_version_id, before.book.current_outline_version_id);
    assert.equal(chapterUnchanged.current_version_id, chapterBefore.current_version_id);
    assert.equal((await json(context, `${bookUrl}/pipeline`)).pipeline.status, 'running');
    await checkNoScriptError();
  } finally {
    await json(context, '/_fixture/version-lock/release', 'post', { width });
  }
  await taskDone(context, started.current_task_id);
  let finished;
  for (let attempt = 0; attempt < 160; attempt++) {
    finished = (await json(context, `${bookUrl}/pipeline`)).pipeline;
    if (['done', 'failed', 'conflict', 'cancelled', 'interrupted'].includes(finished.status)) break;
    await page.waitForTimeout(100);
  }
  assert.equal(finished.status, 'done', JSON.stringify(finished));
  const saved = await json(context, bookUrl);
  assert.equal(saved.setup.worldview, fixture.world.trim());
  assert.equal(saved.book.setup_versions.length, 3, '原生成只写回一次新设定版本');
  await versionControls(false);
  await managementControls(false);
  await page.locator('#writerMoreBtn').click();
  await page.locator('#writerManageBtn').click();
  const activate = page.locator(`[data-writer-manage-use="version:${before.book.setup_versions[0].id}"]`);
  assert.equal(await activate.isEnabled(), true);
  const switching = page.waitForResponse(response => new URL(response.url()).pathname === `${bookUrl}/versions/select`);
  await activate.click();
  assert.equal((await switching).status(), 200, '结束后通过管理记录正常切换版本');
  assert.equal((await json(context, bookUrl)).book.current_setup_version_id, before.book.setup_versions[0].id);
  await page.locator('#writerManageClose').click();
  await checkNoScriptError();
  console.log(`Writer版本锁 ${width}px：四处版本控件、三类管理记录、HTTP 409、原生成完成及恢复切换通过`);
}
async function run(browser, source) {
  const width = source.width;
  const context = await browser.newContext({ viewport: { width, height: width === 375 ? 812 : 900 }, acceptDownloads: true });
  await context.addInitScript(() => {
    localStorage.setItem('naibaOnboardingDismissed', '1');
    localStorage.setItem('naibaChatToken', 'txt-download-fixture');
  });
  const page = await context.newPage(), errors = [];
  if (process.env.NAIBA_TXT_ONLY_WRITER_VERSION_LOCK || process.env.NAIBA_TXT_ONLY_WRITER_CANCEL) {
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) return route.continue();
      errors.push(`隔离验收拒绝外部请求 ${url.origin}`);
      return route.abort();
    });
  }
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !(process.env.NAIBA_TXT_ONLY_WRITER_CANCEL && message.text().includes('Failed to load resource'))) errors.push(message.text());
  });
  page.on('response', response => {
    const pathname = new URL(response.url()).pathname;
    const expectedCancelConflict = process.env.NAIBA_TXT_ONLY_WRITER_CANCEL && response.status() === 409
      && /\/pipeline\/(cancel|retry)$/.test(pathname);
    if (response.status() >= 400 && !pathname.endsWith('/favicon.ico') && !expectedCancelConflict) errors.push(`${response.status()} ${response.url()}`);
  });
  page.on('dialog', dialog => dialog.accept());
  const checkNoScriptError = async () => {
    assert.deepEqual(errors, []);
    assert.equal(await page.locator('body').evaluate(node => /JS ERROR|PROMISE REJECT/.test(node.innerText)), false);
  };
  const artifactBar = anchor => page.locator(anchor).locator('xpath=preceding-sibling::*[1]');
  const assertDownload = async (bar, task, expected, label) => {
    const waiting = page.waitForEvent('download');
    const requesting = page.waitForRequest(request => /\/artifact\?download=1$/.test(request.url()));
    await bar.getByRole('button', { name: /下载.*TXT/ }).click();
    assert.equal((await requesting).headers().authorization, 'Bearer txt-download-fixture', 'TXT fetch显式携带会话Bearer');
    const download = await waiting;
    const destination = path.join(OUTPUT, `${label}-${width}.txt`);
    await download.saveAs(destination);
    const bytes = fs.readFileSync(destination);
    assert.equal(bytes.toString('utf8'), expected);
    if (task.result_artifact.status === 'final') {
      assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), task.result_artifact.sha256);
    }
    await checkNoScriptError();
  };
  const writerResume = async () => {
    const book = source.writer_resume;
    const started = (await json(context, `/api/writer/books/${book.id}/pipelines`, 'post', {
      kind: 'setup_step', step: 'world', model_key: 'online:txt-fixture', model_name: 'txt-fixture',
      extra_note: `TXT_WPARTIAL_${width}`,
    })).pipeline;
    await enter(page, 'writer', book);
    const partial = await taskDone(context, started.current_task_id, 'failed');
    assert.equal(partial.result_artifact.status, 'partial');
    let stopped;
    for (let attempt = 0; attempt < 160; attempt++) {
      stopped = (await json(context, `/api/writer/books/${book.id}/pipeline`)).pipeline;
      if (stopped.status === 'failed') break;
      await page.waitForTimeout(100);
    }
    assert.equal(stopped?.status, 'failed', JSON.stringify(stopped));
    // 刷新后仍须能从持久current_task_id补读TXT状态，不能依赖终态过渡窗口。
    await page.reload({ waitUntil: 'networkidle' });
    await enter(page, 'writer', book);
    await pane(page, 'writer', 'notes');
    const bar = artifactBar('#writerPipelineStream');
    await bar.getByRole('button', { name: '继续未完成内容' }).waitFor({ state: 'visible' });
    await bar.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(OUTPUT, `writer-partial-${width}.png`) });
    await checkNoScriptError();
    const before = await json(context, `/api/writer/books/${book.id}`);
    assert.equal(before.book.setup_versions.length, 1, 'partial尚未创建世界观版本');
    const waiting = page.waitForResponse(response => new URL(response.url()).pathname === `/api/writer/books/${book.id}/pipeline/resume`);
    await bar.getByRole('button', { name: '继续未完成内容' }).click();
    const response = await waiting;
    assert.ok(response.ok(), await response.text());
    const resumed = (await response.json()).pipeline;
    assert.equal(resumed.attempt, started.attempt);
    const childId = resumed.current_task_id || resumed.result_resume?.child_task_id;
    assert.ok(childId && childId !== partial.id, JSON.stringify(resumed));
    const child = await taskDone(context, childId);
    assert.equal(child.logical_generation_id, partial.logical_generation_id);
    assert.equal(child.input.writer.attempt, partial.input.writer.attempt);
    assert.equal(child.result, fixture.world, 'Writer接续只补未完成尾文');
    let pipeline;
    for (let attempt = 0; attempt < 160; attempt++) {
      pipeline = (await json(context, `/api/writer/books/${book.id}/pipeline`)).pipeline;
      if (['done', 'failed', 'conflict', 'cancelled', 'interrupted'].includes(pipeline.status)) break;
      await page.waitForTimeout(100);
    }
    assert.equal(pipeline.status, 'done', JSON.stringify(pipeline));
    assert.equal(pipeline.attempt, started.attempt);
    let saved = await json(context, `/api/writer/books/${book.id}`);
    assert.equal(saved.setup.worldview, fixture.world.trim());
    assert.equal(saved.book.setup_versions.length, 2);
    assert.equal(saved.book.setup_versions.at(-1).task_id, partial.logical_generation_id);
    // 再发同一恢复命令仅返回已提交结果，不能新建任务/版本或再次请求模型。
    const command = response.request().postDataJSON();
    const duplicate = (await json(context, `/api/writer/books/${book.id}/pipeline/resume`, 'post', command)).pipeline;
    assert.equal(duplicate.status, 'done');
    saved = await json(context, `/api/writer/books/${book.id}`);
    assert.equal(saved.book.setup_versions.length, 2);
    await checkNoScriptError();
    console.log(`Writer TXT恢复 ${width}px：界面继续→真实调度→同attempt/logicalID写回一次通过`);
  };
  const generateOutline = async (marker, expectedStatus = 'done') => {
    await page.locator('#novelStageNote').fill(marker);
    await page.waitForFunction(() => !document.querySelector('#novelStageGenerate').disabled);
    const waiting = page.waitForResponse(response => new URL(response.url()).pathname === '/api/novel/stage' && response.request().method() === 'POST');
    await page.locator('#novelStageGenerate').click();
    const response = await waiting;
    assert.ok(response.ok(), await response.text());
    const { task } = await response.json();
    const finished = await taskDone(context, task.id, expectedStatus);
    await checkNoScriptError();
    return finished;
  };
  try {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await page.evaluate(async () => {
      await import(document.querySelector('script[src*="/js/main.js"]').src);
    });
    if (process.env.NAIBA_TXT_ONLY_WRITER_CANCEL) {
      await writerCancellation(page, context, source, checkNoScriptError);
      return;
    }
    if (process.env.NAIBA_TXT_ONLY_WRITER_VERSION_LOCK) {
      await writerVersionLock(page, context, source, checkNoScriptError);
      return;
    }
    if (process.env.NAIBA_TXT_ONLY_WRITER_RESUME) {
      await writerResume();
      return;
    }
    if (!process.env.NAIBA_TXT_ONLY_WRITER) {
    await enter(page, 'novel', source.novel);
    await page.locator('#novelStages [data-novel-stage="outline"]').click();
    await pane(page, 'novel', 'chapters');
    await page.locator('[data-novel-pick="1"]').check();
    await pane(page, 'novel', 'editor');
    const outline = await generateOutline(`TXT_OUTLINE_${width}`);
    assert.equal(outline.result_artifact.status, 'final');
    await page.waitForFunction(() => document.querySelector('#novelStageTask .wb-result-artifact')?.textContent.includes('TXT 已完成'));
    const outlineBar = artifactBar('#novelStageResult');
    assert.equal(await outlineBar.evaluate(node => {
      const rect = node.getBoundingClientRect(); return rect.left >= -1 && rect.right <= innerWidth + 1;
    }), true, 'TXT控件不能横向溢出视口');
    await outlineBar.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(OUTPUT, `workbench-outline-${width}.png`) });
    await outlineBar.getByRole('button', { name: '查看 TXT' }).click();
    await page.locator('.wb-result-preview[open]').waitFor();
    assert.equal(await page.locator('.wb-result-preview pre').textContent(), fixture.outline);
    await page.locator('.wb-result-preview').getByRole('button', { name: '关闭' }).click();
    await checkNoScriptError();
    await assertDownload(outlineBar, outline, fixture.outline, 'outline');
    await page.locator('#novelStageApply').click();
    await page.waitForFunction(() => document.querySelector('#novelStageSavedBody')?.textContent.includes('旧船调查'));
    const saved = await json(context, `/api/novel/books/${source.novel.id}`);
    assert.equal(saved.book.outline, fixture.outline);
    await page.reload({ waitUntil: 'networkidle' }); await enter(page, 'novel', source.novel);
    await page.locator('#novelStages [data-novel-stage="outline"]').click(); await pane(page, 'novel', 'editor');
    assert.ok((await page.locator('#novelStageSavedBody').textContent()).includes('旧船调查'));
    await checkNoScriptError();

    // 第一段已确认后网络中断。partial不可写回；显式继续保留logicalID、只补第二段。
    await pane(page, 'novel', 'chapters'); await page.locator('[data-novel-pick="1"]').check();
    await pane(page, 'novel', 'editor');
    const partial = await generateOutline(`TXT_PARTIAL_${width}`, 'failed');
    assert.equal(partial.result_artifact.status, 'partial');
    await page.waitForFunction(() => document.querySelector('#novelStageTask .wb-result-artifact')?.textContent.includes('TXT 未完成'));
    await artifactBar('#novelStageResult').getByRole('button', { name: '继续未完成内容' }).waitFor({ state: 'visible' });
    await artifactBar('#novelStageResult').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(OUTPUT, `workbench-partial-${width}.png`) });
    assert.equal(await page.locator('#novelStageApply').isVisible(), false);
    const rejected = await context.request.post(`${BASE}/api/novel/books/${source.novel.id}/apply`, {
      data: { stage: 'outline', task_id: partial.id, text: '不得保存此伪造结果', revision: saved.book.revision },
    });
    assert.equal(rejected.status(), 409);
    const oldArtifact = await json(context, `/api/studio/tasks/${partial.id}/artifact`);
    assert.ok(fixture.outline.startsWith(oldArtifact.text));
    const resuming = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/resume-result'));
    await artifactBar('#novelStageResult').getByRole('button', { name: '继续未完成内容' }).click();
    const { task: child } = await (await resuming).json();
    assert.notEqual(child.id, partial.id);
    assert.equal(child.logical_generation_id, partial.logical_generation_id);
    const resumed = await taskDone(context, child.id);
    assert.equal(resumed.result, fixture.outline, '恢复只拼接缺少的第二段，不重复前文');
    await page.waitForFunction(() => document.querySelector('#novelStageTask .wb-result-artifact')?.textContent.includes('TXT 已完成'));
    await assertDownload(artifactBar('#novelStageResult'), resumed, fixture.outline, 'resumed');
    await checkNoScriptError();

    // 完成文件损坏后旧result不能成为保存后门。
    await json(context, '/_fixture/corrupt', 'post', { task_id: resumed.id });
    const bad = await json(context, `/api/studio/tasks/${resumed.id}`);
    assert.equal(bad.task.result_artifact.status, 'invalid');
    const badSave = await context.request.post(`${BASE}/api/novel/books/${source.novel.id}/apply`, {
      data: { stage: 'outline', task_id: resumed.id, text: fixture.outline, revision: saved.book.revision },
    });
    assert.equal(badSave.status(), 409);
    await checkNoScriptError();

    // 单章AI改编使用manuscripts保存入口，文件原文为服务端权威来源。
    await page.locator('#novelStages [data-novel-stage="split"]').click();
    await pane(page, 'novel', 'chapters');
    await page.locator('[data-novel-chapter="1"]').click(); await pane(page, 'novel', 'editor');
    await page.locator('[data-novel-manuscript-action="ai"]').first().click();
    await page.locator('#novelAiRequest').fill(`TXT_ADAPT_${width}`);
    const adapting = page.waitForResponse(response => new URL(response.url()).pathname === '/api/novel/stage' && response.request().method() === 'POST');
    await page.locator('#novelAiSubmit').click();
    const { task: adaptTask } = await (await adapting).json();
    const adapted = await taskDone(context, adaptTask.id);
    await page.waitForFunction(() => document.querySelector('#novelAiSubmit')?.textContent.includes('保存改编稿'));
    await assertDownload(artifactBar('#novelAiResult'), adapted, fixture.adapt, 'adapt');
    const savingAdapt = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/manuscripts') && response.request().method() === 'POST');
    await page.locator('#novelAiSubmit').click();
    const adaptSave = await savingAdapt;
    assert.ok(adaptSave.ok(), await adaptSave.text());
    const manuscript = (await adaptSave.json()).manuscript;
    assert.equal(manuscript.task_id, adapted.logical_generation_id);
    await checkNoScriptError();

    }
    // Writer单步世界观经过真实流水线自动写回；生成记录提供TXT操作。
    await json(context, `/api/writer/books/${source.writer.id}/pipelines`, 'post', {
      kind: 'setup_step', step: 'world', model_key: 'online:txt-fixture', model_name: 'txt-fixture', extra_note: `TXT_WORLD_${width}`,
    });
    await enter(page, 'writer', source.writer);
    let worldPipeline;
    for (let attempt = 0; attempt < 160; attempt++) {
      worldPipeline = (await json(context, `/api/writer/books/${source.writer.id}/pipeline`)).pipeline;
      if (['done', 'failed', 'interrupted', 'cancelled', 'conflict'].includes(worldPipeline?.status)) break;
      await page.waitForTimeout(100);
    }
    assert.equal(worldPipeline?.status, 'done', JSON.stringify(worldPipeline));
    const writerBook = await json(context, `/api/writer/books/${source.writer.id}`);
    fs.writeFileSync(path.join(OUTPUT, `writer-world-${width}.json`), JSON.stringify(writerBook, null, 2));
    assert.equal(writerBook.setup.worldview, fixture.world.trim());
    const list = await json(context, '/api/studio/tasks?module=writer');
    const worldTask = list.tasks.find(task => task.writer?.book_id === source.writer.id || task.input?.writer?.book_id === source.writer.id);
    assert.ok(worldTask);
    const world = await taskDone(context, worldTask.id);
    await page.locator('#writerMoreBtn').click(); await page.locator('#writerManageBtn').click();
    await page.locator('[data-writer-manage-tab="task"]').click();
    await page.locator(`[data-writer-manage-open="task:${world.id}"]`).click();
    await assertDownload(artifactBar('#writerManagePreviewBody'), world, fixture.world, 'world');
    await page.locator('#writerManageClose').click(); await checkNoScriptError();

    // 浏览器手改保存；再检查服务端对CRLF/emoji的无损保存及刷新重放。
    await page.locator('#writerStages [data-writer-stage="draft"]').click();
    await pane(page, 'writer', 'book');
    await page.locator('[data-writer-chapter="1"]').click(); await pane(page, 'writer', 'editor');
    await page.waitForFunction(() => document.querySelector('#writerDraftTitle')?.textContent.includes('来信'));
    const manual = `手工正文${width}：港口的月亮🌙\n\n新的一行保留空白。`;
    await page.locator('#writerDraftContent').fill(manual);
    const savingManual = page.waitForResponse(response => /\/api\/writer\/books\/.*\/chapters\/1$/.test(new URL(response.url()).pathname) && response.request().method() === 'PATCH');
    await page.locator('#writerDraftSave').click(); assert.ok((await savingManual).ok());
    let chapter = await json(context, `/api/writer/books/${source.writer.id}/chapters/1`);
    assert.equal(chapter.content, manual);
    const crlf = `  CRLF${width}🌙\r\n\r\n尾行保留空格  \r\n`;
    await json(context, `/api/writer/books/${source.writer.id}/chapters/1`, 'patch', { content: crlf, revision: chapter.revision });
    chapter = await json(context, `/api/writer/books/${source.writer.id}/chapters/1`);
    assert.equal(chapter.content, crlf);
    await page.reload({ waitUntil: 'networkidle' }); await enter(page, 'writer', source.writer);
    await page.locator('#writerStages [data-writer-stage="draft"]').click();
    await pane(page, 'writer', 'book'); await page.locator('[data-writer-chapter="1"]').click(); await pane(page, 'writer', 'editor');
    await page.waitForFunction(expected => document.querySelector('#writerDraftContent')?.value === expected, crlf.replaceAll('\r\n', '\n'));
    assert.equal(await page.locator('#writerDraftContent').inputValue(), crlf.replaceAll('\r\n', '\n'), 'textarea按HTML规范显示换行，服务端原始CRLF保留');
    await checkNoScriptError();
    await page.screenshot({ path: path.join(OUTPUT, `workbench-txt-${width}.png`) });
    await writerResume();
    console.log(`TXT ${width}px：outline/adapt/world、下载hash、partial恢复、invalid拒保存、人工正文与CRLF通过`);
  } catch (error) {
    await page.screenshot({ path: path.join(OUTPUT, `workbench-txt-failure-${width}.png`) }).catch(() => {});
    fs.writeFileSync(path.join(OUTPUT, `failure-${width}.txt`), (await page.locator('body').innerText().catch(() => '')) + '\n\n' + errors.join('\n'));
    throw error;
  } finally { await context.close(); }
}
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try { for (const source of fixture.sources) await run(browser, source); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
