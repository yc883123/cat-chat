// The harness seeds current chapter schema with legacy outline fields. No API route mocks.
const { chromium } = require('playwright');
const strict = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let assertions = 0;
function assert(...args) { assertions += 1; return strict(...args); }
for (const name of ['equal', 'deepEqual']) {
  assert[name] = (...args) => { assertions += 1; return strict[name](...args); };
}
const fixturePath = process.env.NAIBA_OUTLINE_CONTINUE_SOURCE;
assert(fixturePath, 'run through the isolated source harness');
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(fixture.base));
const output = path.resolve(process.env.NAIBA_SMOKE_OUTPUT || path.dirname(fixturePath));
const manifest = book => JSON.parse(fs.readFileSync(book.manifest, 'utf8'));

async function get(context, endpoint) {
  const response = await context.request.get(fixture.base + endpoint);
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

function previewFor(response, source, value) {
  if (!response.url().endsWith('/api/novel/stage/preview')) return false;
  const body = response.request().postDataJSON();
  return body.book_id === source.id && body.stage === 'outline'
    && Number(body.continue_outline_version || 0) === value
    && body.chapter_ids?.length === 2;
}

async function runViewport(browser, viewport, source) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const errors = [];
  const responses = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() !== 'error') return;
    const url = message.location().url || '';
    if (url === fixture.base + '/favicon.ico' && message.text().includes('404')) return;
    errors.push(`${message.text()} ${url}`);
  });
  page.on('response', response => {
    if (response.url().includes('/api/novel/stage') || response.url().endsWith('/apply')) {
      responses.push({ url: response.url(), status: response.status() });
    }
  });
  page.on('dialog', dialog => dialog.accept());
  try {
    const bootstrap = await get(context, '/api/bootstrap');
    assert.equal(bootstrap.data_location.is_frozen, false);
    assert.equal(path.resolve(bootstrap.data_location.data_dir).toLowerCase(),
      path.resolve(fixture.runtime, 'data').toLowerCase());
    assert.equal((bootstrap.mcp_servers || []).length, 0);
    await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
    await page.goto(fixture.base, { waitUntil: 'networkidle' });
    assert.equal(await page.title(), 'Cat Chat');
    await enter(page, source);
    await page.locator('#novelStages [data-novel-stage="outline"]').click();
    await pane(page, 'chapters');
    for (const no of [2, 3]) await page.locator(`#novelChapterList [data-novel-pick="${no}"]`).check();
    await pane(page, 'editor');
    const select = page.locator('#novelStagePanel [data-outline-continue]');
    await select.waitFor({ state: 'visible' });
    assert.equal(await select.inputValue(), '', 'continuation must default to off');
    assert.deepEqual(await select.locator('option').evaluateAll(rows => rows.map(row => row.value)), ['', '1']);
    const beforePreview = fs.readFileSync(source.manifest);
    const previewResponse = page.waitForResponse(response => previewFor(response, source, 1));
    await select.selectOption('1');
    const preview = await previewResponse;
    assert.equal(preview.status(), 200, await preview.text());
    const previewBody = await preview.json();
    assert.equal(previewBody.sources.continues_outline, true);
    assert.equal(previewBody.sources.continue_outline_version, 1);
    assert.deepEqual(fs.readFileSync(source.manifest), beforePreview, 'preview must be read only');
    assert.equal('outline_versions' in manifest(source), false);
    const sourceLabel = previewBody.sources.continue_outline_label
      || await select.locator('option:checked').textContent();
    await pane(page, 'preview');
    await page.waitForFunction(label => document.querySelector('#novelPlanBody')?.textContent.includes(`接续「${label}」`), sourceLabel);
    assert((await page.locator('#novelPlanBody').innerText()).includes(`接续「${sourceLabel}」`));
    await page.screenshot({ path: path.join(output, `outline_sources_${viewport.width}.png`) });
    await pane(page, 'editor');
    await page.waitForFunction(() => Boolean(document.querySelector('#novelStageModel')?.value));
    const submitResponse = page.waitForResponse(response => response.url().endsWith('/api/novel/stage'));
    await page.locator('#novelStageGenerate').click();
    const submitted = await submitResponse;
    assert.equal(submitted.status(), 201, await submitted.text());
    const submittedBody = submitted.request().postDataJSON();
    assert.equal(submittedBody.continue_outline_version, 1);
    assert.deepEqual(submittedBody.chapter_ids, [2, 3]);
    const generated = await submitted.json();
    const taskId = generated.task.id;
    assert.equal(generated.sources.continue_outline_version, 1);
    await page.waitForFunction(expected => document.querySelector('#novelStageResult')?.textContent === expected,
      fixture.full_outline);
    await page.locator('#novelStageApply').waitFor({ state: 'visible' });
    const task = (await get(context, `/api/studio/tasks/${taskId}`)).task;
    assert.equal(task.status, 'done');
    assert.equal(task.result, fixture.full_outline);
    assert.equal(task.input.novel.sources.continue_outline_version, 1);
    assert(task.input.content.includes(fixture.old_outline));
    assert(task.input.content.includes('接续要求'));
    assert.equal('outline_versions' in manifest(source), false, 'submit must not synthesize versions');
    await page.screenshot({ path: path.join(output, `outline_result_${viewport.width}.png`) });
    const applyResponse = page.waitForResponse(response => response.url().endsWith(`/api/novel/books/${source.id}/apply`));
    await page.locator('#novelStageApply').click();
    const applied = await applyResponse;
    assert.equal(applied.status(), 200, await applied.text());
    const book = (await applied.json()).book;
    assert.deepEqual(book.outline_versions.map(row => row.version), [1, 2]);
    assert.equal(book.outline_versions[0].text, fixture.old_outline);
    assert.equal(book.outline_versions[1].text, fixture.full_outline);
    assert.equal(book.outline_versions[1].task_id, taskId);
    await page.waitForFunction(() => document.querySelector('#novelStageVersion')?.querySelectorAll('option').length === 2);
    await page.locator('#novelStageVersion').selectOption('1');
    assert.equal(await page.locator('#novelStageSavedBody').textContent(), fixture.old_outline);
    assert.deepEqual(await select.locator('option').evaluateAll(rows => rows.map(row => row.value)), ['', '1', '2']);
    const persisted = manifest(source);
    assert.equal(persisted.outline_versions[0].text, fixture.old_outline);
    assert.equal(persisted.outline_versions[1].task_id, taskId);
    await page.screenshot({ path: path.join(output, `outline_saved_${viewport.width}.png`) });
    const offPreview = page.waitForResponse(response => previewFor(response, source, 0));
    await select.selectOption('');
    assert.equal((await offPreview).status(), 200);
    assert.equal(await select.inputValue(), '');
    await select.selectOption('1');
    await enter(page, fixture.other);
    await pane(page, 'editor');
    assert.equal(await select.inputValue(), '', 'switching books must clear continuation');
    const bounds = await select.boundingBox();
    assert(bounds && bounds.width > 0 && bounds.x >= 0 && bounds.x + bounds.width <= viewport.width + 1,
      'continuation dropdown must fit the viewport');
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'page overflows horizontally');
    assert.deepEqual(errors, [], 'unexpected browser errors');
    assert(responses.length > 3 && responses.every(response => response.status < 400),
      JSON.stringify(responses));
    console.log(`${viewport.width}px: real legacy preview, queued generation, three-act output, save v2, unchanged v1, reset all passed`);
  } catch (error) {
    await page.screenshot({ path: path.join(output, `outline_failed_${viewport.width}.png`) }).catch(() => {});
    throw error;
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    await runViewport(browser, { width: 1280, height: 900 }, fixture.books[0]);
    await runViewport(browser, { width: 375, height: 812 }, fixture.books[1]);
    const calls = JSON.parse(fs.readFileSync(fixture.model_calls, 'utf8'));
    assert.equal(calls.length, 2, 'exactly two real runner invocations must reach the deterministic model boundary');
    console.log(`outline continuation: ${assertions} assertions passed; deterministic model output validates flow only`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
