// Isolated source service, real Novel/Studio APIs; only the external model is deterministic.
const { chromium } = require('playwright');
const strict = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let assertions = 0;
function assert(...args) { assertions += 1; return strict(...args); }
for (const key of ['equal', 'notEqual', 'deepEqual']) {
  assert[key] = (...args) => { assertions += 1; return strict[key](...args); };
}
const fixturePath = process.env.NAIBA_STORYBOARD_VERSIONS_SOURCE;
assert(fixturePath, 'run through the isolated source harness');
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const BASE = fixture.base;
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(BASE));
const OUTPUT = path.resolve(process.env.NAIBA_SMOKE_OUTPUT || path.dirname(fixturePath));

async function get(context, endpoint) {
  const response = await context.request.get(BASE + endpoint);
  assert.equal(response.status(), 200, `${endpoint}: ${await response.text()}`);
  return response.json();
}

async function enter(page, source) {
  await page.evaluate(async bookId => {
    const studio = await import('/js/22-studio.js');
    studio.openStudioView();
    studio.setStudioPage('novel');
    window.dispatchEvent(new CustomEvent('naiba-open-domain', { detail: { domain: 'novel', bookId } }));
  }, source.id);
  await page.waitForFunction(title => document.querySelector('#novelBookTitle')?.textContent.includes(title), source.title);
}

async function pane(page, name) {
  const button = page.locator(`#novelPanes [data-novel-pane="${name}"]`);
  if (await button.isVisible()) await button.click();
}

function previewFor(response, source, version) {
  if (!response.url().endsWith('/api/novel/stage/preview') || response.request().method() !== 'POST') return false;
  const body = response.request().postDataJSON();
  return body.book_id === source.id && body.stage === 'storyboard' && Number(body.script_version) === version;
}

async function previewResponse(response, version, count) {
  assert.equal(response.status(), 200, await response.text());
  const body = await response.json();
  assert.equal(body.sources.script_version, version, 'preflight must identify the selected script version');
  assert.equal(body.sources.scene_count, count, 'preflight must count scenes from the selected version');
  assert.deepEqual(body.sources.scene_ids, Array.from({ length: count }, (_, index) => index + 1));
  return body;
}

async function chooseVersion(page, source, version, count) {
  const response = page.waitForResponse(row => previewFor(row, source, version));
  await page.locator('#novelStoryboardScriptVersion').selectOption(String(version));
  const result = await previewResponse(await response, version, count);
  assert.equal(await page.locator('#novelStoryboardScriptVersion').inputValue(), String(version));
  assert((await page.locator('#novelStageMeta').textContent()).includes(`${count} 场`));
  return result;
}

// Fetch each response from the actual source service, then delay only browser delivery.
async function deferRealResponse(page, pattern, matches) {
  let pending = true, releaseResponse = null, resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });
  await page.route(pattern, async route => {
    if (!pending || !matches(route.request())) return route.continue();
    pending = false;
    const response = await route.fetch();
    const body = await response.body();
    let finish;
    const delivered = new Promise(resolve => { finish = resolve; });
    let released = false;
    releaseResponse = async () => {
      if (released) return;
      released = true;
      try { await route.fulfill({ response, body }); } finally { finish(); }
    };
    resolveReady(JSON.parse(body.toString('utf8')));
    await delivered;
  });
  return { ready, release: async () => { if (releaseResponse) await releaseResponse(); } };
}

async function checkViewport(browser, viewport, sources) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [], stageResponses = [], heldResponses = [];
  const source = sources.multi;
  const version = page.locator('#novelStoryboardScriptVersion');
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  page.on('response', response => {
    const url = new URL(response.url());
    if (response.status() >= 400 && !url.pathname.endsWith('/favicon.ico')) errors.push(`HTTP ${response.status()} ${url.pathname}`);
    if (url.pathname.startsWith('/api/novel/stage')) stageResponses.push({ url: url.pathname, status: response.status() });
  });
  page.on('dialog', dialog => dialog.accept());
  try {
    const bootstrap = await get(context, '/api/bootstrap');
    assert.equal(bootstrap.data_location.is_frozen, false);
    assert.equal(path.resolve(bootstrap.data_location.data_dir).toLowerCase(),
      path.resolve(fixture.runtime, 'data').toLowerCase());
    assert.equal((bootstrap.mcp_servers || []).length, 0);
    await context.addInitScript(() => {
      localStorage.clear();
      localStorage.setItem('naibaOnboardingDismissed', '1');
    });
    await page.goto(BASE, { waitUntil: 'networkidle' });
    assert.equal(await page.title(), 'Cat Chat');

    // A single saved version and a legacy manifest both expose the one actual source.
    for (const [kind, count] of [['single', 1], ['legacy', 2]]) {
      await enter(page, sources[kind]);
      const response = page.waitForResponse(row => previewFor(row, sources[kind], 1));
      await page.locator('#novelStages [data-novel-stage="storyboard"]').click();
      await pane(page, 'editor');
      await version.waitFor({ state: 'visible' });
      await previewResponse(await response, 1, count);
      assert.equal(await version.inputValue(), '1', `${kind} source must use the only effective version`);
      assert.deepEqual(await version.locator('option').evaluateAll(rows => rows.map(row => row.value)), ['1']);
      assert.equal(await page.locator('#novelStageGenerate').isEnabled(), true);
    }

    await enter(page, source);
    await page.locator('#novelStages [data-novel-stage="script"]').click();
    await pane(page, 'editor');
    await page.locator('#novelScriptVersion').selectOption('1');
    assert.equal(await page.locator('#novelSceneList .novel-scene-row').count(), 2);
    const oldHeads = await page.locator('#novelSceneList .novel-scene-open strong').allTextContents();
    assert(oldHeads.every(text => text.includes('旧港')), 'script preview itself must show the old scenes');
    const manifestBeforePreview = fs.readFileSync(source.manifest);
    const savedOldScenes = JSON.parse(manifestBeforePreview.toString('utf8')).script_versions[0].script;
    const oldPreviewResponse = page.waitForResponse(row => previewFor(row, source, 1));
    await page.locator('#novelStages [data-novel-stage="storyboard"]').click();
    await pane(page, 'editor');
    const oldPreview = await previewResponse(await oldPreviewResponse, 1, 2);
    assert.equal(await version.inputValue(), '1', 'switching from the script pane must retain the chosen old version');
    assert.deepEqual(await version.locator('option').evaluateAll(rows => rows.map(row => row.value)), ['1', '2']);
    const chapterNote = await page.locator('#novelChapterNote').textContent();
    assert(chapterNote.includes('第 1 版') && chapterNote.includes('2 场'),
      'chapter column note must use the selected old version and scene count');
    assert.deepEqual(fs.readFileSync(source.manifest), manifestBeforePreview, 'preflight must not rewrite the book');
    await page.screenshot({ path: path.join(OUTPUT, `storyboard_editor_${viewport.width}.png`) });
    await pane(page, 'preview');
    const planBefore = await page.locator('#novelPlanBody').innerText();
    assert(planBefore.includes('第 1 版') && planBefore.includes('2 场'), 'source plan must show the chosen old version and its scene count');
    assert(planBefore.includes('所选剧本版本') && !planBefore.includes('已保存的最新场次'),
      'fixed stage description must agree with the explicitly selected script version');
    await page.screenshot({ path: path.join(OUTPUT, `storyboard_sources_${viewport.width}.png`) });
    await pane(page, 'editor');
    await page.waitForFunction(() => Boolean(document.querySelector('#novelStageModel')?.value));
    const oldSubmitGate = await deferRealResponse(page, '**/api/novel/stage', request => {
      const body = request.postDataJSON();
      return body.book_id === source.id && body.stage === 'storyboard' && body.script_version === 1;
    });
    heldResponses.push(oldSubmitGate);
    const firstSubmission = page.waitForResponse(response => response.url().endsWith('/api/novel/stage')
      && response.request().postDataJSON().stage === 'storyboard');
    await page.locator('#novelStageGenerate').click();
    await oldSubmitGate.ready;
    await chooseVersion(page, source, 2, 3);
    await oldSubmitGate.release();
    const firstResponse = await firstSubmission;
    assert.equal(firstResponse.status(), 201, await firstResponse.text());
    assert.equal(firstResponse.request().postDataJSON().script_version, 1, 'UI submission must explicitly carry the old version');
    const first = await firstResponse.json();
    assert.equal(first.sources.script_version, 1);
    await page.waitForFunction(() => document.querySelector('#novelStageTaskState')?.textContent.includes('完成')
      && document.querySelectorAll('#novelStoryboardList .novel-storyboard-row').length === 2);
    const oldTask = (await get(context, `/api/studio/tasks/${first.task.id}`)).task;
    assert.equal(oldTask.status, 'done');
    assert.equal(oldTask.input.novel.script_version, 1);
    assert.equal(oldTask.input.novel.sources.script_version, 1);
    assert.equal(oldTask.input.novel.scene_snapshot.length, 2);
    for (const marker of fixture.old_markers) assert(oldTask.input.content.includes(marker), `frozen input must contain ${marker}`);
    for (const marker of [...fixture.current_markers, ...fixture.generated_markers]) assert(!oldTask.input.content.includes(marker), `frozen old input must exclude ${marker}`);
    assert.equal(oldPreview.budget.chars,
      Array.from(oldTask.input.content).length + Array.from(oldTask.input.system || '').length,
      'preflight budget must describe the same old body that is frozen for submission');
    assert((await page.locator('#novelStageTaskSources').textContent()).includes('第 1 版'));
    assert.equal(await version.inputValue(), '2', 'a late v1 submission response must retain the newly selected v2 source');
    await pane(page, 'preview');
    const currentMaterial = page.locator('#novelPlanBody .novel-plan-block')
      .filter({ has: page.locator('strong', { hasText: '本次素材' }) });
    const currentMaterialText = await currentMaterial.innerText();
    assert(currentMaterialText.includes('第 2 版') && currentMaterialText.includes('3 场'),
      'a late v1 submission response must not overwrite v2 preflight sources');
    await page.screenshot({ path: path.join(OUTPUT, `storyboard_submit_race_${viewport.width}.png`) });
    await pane(page, 'editor');

    // Create and save a genuinely new script task through the UI, keeping old v1 intact.
    await page.locator('#novelStages [data-novel-stage="script"]').click();
    await pane(page, 'chapters');
    await page.locator('#novelChapterList [data-novel-pick="1"]').check();
    await pane(page, 'editor');
    const scriptSubmission = page.waitForResponse(response => response.url().endsWith('/api/novel/stage')
      && response.request().postDataJSON().stage === 'script');
    await page.locator('#novelStageGenerate').click();
    const scriptResponse = await scriptSubmission;
    assert.equal(scriptResponse.status(), 201, await scriptResponse.text());
    await page.waitForFunction(expected => document.querySelector('#novelStageResult')?.textContent === expected,
      fixture.generated_script);
    const applyResponse = page.waitForResponse(response => response.url().endsWith(`/api/novel/books/${source.id}/apply`));
    await page.locator('#novelStageApply').click();
    const applied = await applyResponse;
    assert.equal(applied.status(), 200, await applied.text());
    const updated = (await applied.json()).book;
    assert.deepEqual(updated.script_versions.map(row => row.version), [1, 2, 3]);
    assert.equal(updated.script_versions[0].script.length, 2);
    assert.equal(updated.script_versions[2].script.length, 4);
    assert.deepEqual(updated.script_versions[0].script, savedOldScenes,
      'creating a new version must preserve the complete old version content');
    const thirdPreviewResponse = page.waitForResponse(row => previewFor(row, source, 3));
    await page.locator('#novelStages [data-novel-stage="storyboard"]').click();
    await pane(page, 'editor');
    await previewResponse(await thirdPreviewResponse, 3, 4);
    assert.equal(await version.inputValue(), '3', 'newly saved script becomes the default source');
    assert.deepEqual(await version.locator('option').evaluateAll(rows => rows.map(row => row.value)), ['1', '2', '3']);
    assert((await page.locator('#novelStageTaskSources').textContent()).includes('第 1 版'),
      'editing the next source must not relabel an existing task');
    await chooseVersion(page, source, 1, 2);
    await chooseVersion(page, source, 3, 4);

    // Let a real running task be discovered from the summary list after switching books.
    // Hold its first real detail response while the normal task poll observes completion.
    assert.equal((await context.request.post(`${BASE}/_fixture/model-hold`, { data: {} })).status(), 200);
    const newSubmitGate = await deferRealResponse(page, '**/api/novel/stage', request => {
      const body = request.postDataJSON();
      return body.book_id === source.id && body.stage === 'storyboard' && body.script_version === 3;
    });
    heldResponses.push(newSubmitGate);
    const newSubmission = page.waitForResponse(response => response.url().endsWith('/api/novel/stage')
      && response.request().postDataJSON().stage === 'storyboard');
    await page.locator('#novelStageGenerate').click();
    const pendingTask = (await newSubmitGate.ready).task;
    await enter(page, sources.single);
    await newSubmitGate.release();
    const newResponse = await newSubmission;
    assert.equal(newResponse.status(), 201, await newResponse.text());
    const latest = await newResponse.json();
    assert.equal(latest.sources.script_version, 3);
    const detailGate = await deferRealResponse(page, '**/api/studio/tasks/*', request =>
      request.method() === 'GET' && new URL(request.url()).pathname === `/api/studio/tasks/${pendingTask.id}`);
    heldResponses.push(detailGate);
    await enter(page, source);
    await pane(page, 'editor');
    const staleDetail = (await detailGate.ready).task;
    assert(['queued', 'running'].includes(staleDetail.status), 'the delayed detail must be a true pre-completion backend response');
    assert.equal((await context.request.post(`${BASE}/_fixture/model-release`, { data: {} })).status(), 200);
    await page.waitForFunction(() => document.querySelector('#novelStageTaskState')?.textContent.includes('完成')
      && document.querySelectorAll('#novelStoryboardList .novel-storyboard-row').length === 4);
    await detailGate.release();
    await page.waitForTimeout(150);
    assert((await page.locator('#novelStageTaskState').textContent()).includes('完成'),
      'a late running detail must not overwrite a completed poll state');
    assert.equal(await page.locator('#novelStoryboardList .novel-storyboard-row').count(), 4,
      'a late running detail must not erase completed storyboard results');
    assert((await page.locator('#novelStageTaskSources').textContent()).includes('第 3 版'));
    const latestTask = (await get(context, `/api/studio/tasks/${latest.task.id}`)).task;
    assert.equal(latestTask.input.novel.script_version, 3);
    for (const marker of fixture.generated_markers) assert(latestTask.input.content.includes(marker));
    for (const marker of fixture.old_markers) assert(!latestTask.input.content.includes(marker));
    await page.locator(`#novelTaskHistory [data-novel-task="${first.task.id}"]`).click();
    assert.equal(await version.inputValue(), '3', 'viewing an old task must not change the next submission source');
    assert((await page.locator('#novelStageTaskSources').textContent()).includes('第 1 版'));
    assert.equal(await page.locator('#novelStoryboardList .novel-storyboard-row').count(), 2);
    await pane(page, 'preview');
    const historicalPlan = await page.locator('#novelPlanBody').innerText();
    assert(historicalPlan.includes('第 3 版') && historicalPlan.includes('第 1 版'),
      'next source and displayed historical task source must each retain their own version');
    await page.screenshot({ path: path.join(OUTPUT, `storyboard_history_${viewport.width}.png`) });
    await pane(page, 'editor');

    // Delete the selected old script through the real record management UI.
    await chooseVersion(page, source, 1, 2);
    const oldVersionId = updated.script_versions.find(row => row.version === 1).id;
    await page.locator('#novelMoreBtn').click();
    await page.locator('#novelManageBtn').click();
    await page.locator('#novelManageTabs [data-novel-manage-tab="script"]').click();
    const record = page.locator(`#novelManageList [data-novel-manage-row="script:${oldVersionId}"]`);
    await record.check();
    const deletion = page.waitForResponse(response => response.url().endsWith('/api/novel/records/delete'));
    const afterDeletePreview = page.waitForResponse(row => previewFor(row, source, 3));
    await page.locator('#novelManageDelete').click();
    const deleted = await deletion;
    assert.equal(deleted.status(), 200, await deleted.text());
    assert.equal((await deleted.json()).succeeded.length, 1);
    await record.waitFor({ state: 'detached' });
    await page.locator('#novelManageClose').click();
    await previewResponse(await afterDeletePreview, 3, 4);
    assert.equal(await version.inputValue(), '3', 'deleting the selected source must visibly fall back to a valid latest version');
    assert.deepEqual(await version.locator('option').evaluateAll(rows => rows.map(row => row.value)), ['2', '3']);
    assert((await page.locator('#novelStageTaskSources').textContent()).includes('第 1 版'),
      'deleted source versions remain identified in the original task snapshot');
    const frozenAfterDelete = (await get(context, `/api/studio/tasks/${first.task.id}`)).task;
    assert.deepEqual(frozenAfterDelete.input, oldTask.input, 'deleting a script version must not rewrite task input');
    assert.equal(frozenAfterDelete.result, oldTask.result);

    await enter(page, sources.single);
    await pane(page, 'editor');
    await version.waitFor({ state: 'visible' });
    assert.equal(await version.inputValue(), '1', 'switching books must clear an unrelated selected version');
    assert.equal(await page.locator('#novelStageTaskSources').isVisible(), false,
      'switching books must not retain another book task source');
    await enter(page, source);
    await pane(page, 'editor');
    await page.waitForFunction(() => document.querySelector('#novelStoryboardScriptVersion')?.value === '3');
    assert.deepEqual(await version.locator('option').evaluateAll(rows => rows.map(row => row.value)), ['2', '3']);
    const bounds = await version.boundingBox();
    assert(bounds && bounds.x >= 0 && bounds.width > 0 && bounds.x + bounds.width <= viewport.width + 1,
      'source dropdown must fit inside the viewport');
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'page must not overflow horizontally');
    assert.deepEqual(errors, [], 'browser must have no script, console or HTTP errors');
    assert(stageResponses.length > 8 && stageResponses.every(row => row.status < 400), JSON.stringify(stageResponses));
    await page.screenshot({ path: path.join(OUTPUT, `storyboard_final_${viewport.width}.png`) });
    console.log(`${viewport.width}px: single/legacy/multiple script versions, old source preview and frozen input, new script v3, history source, delete fallback, book reset and two delayed-response races passed`);
  } catch (error) {
    console.error('Storyboard version browser errors:', errors);
    await page.screenshot({ path: path.join(OUTPUT, `storyboard_failed_${viewport.width}.png`) }).catch(() => {});
    throw error;
  } finally {
    await Promise.allSettled(heldResponses.map(response => response.release()));
    await context.request.post(`${BASE}/_fixture/model-release`, { data: {} }).catch(() => {});
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const [index, viewport] of [{ width: 1280, height: 900 }, { width: 375, height: 812 }].entries()) {
      await checkViewport(browser, viewport, fixture.sets[index]);
    }
    const calls = JSON.parse(fs.readFileSync(fixture.model_calls, 'utf8'));
    assert.equal(calls.length, 6, 'each viewport submits two storyboard tasks and one new script task');
    assert.equal(calls.filter(row => row.stage === 'script').length, 2);
    assert.equal(calls.filter(row => row.stage === 'storyboard').length, 4);
    console.log(`storyboard versions: ${assertions} assertions passed; model boundary is deterministic and does not evaluate model quality`);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
