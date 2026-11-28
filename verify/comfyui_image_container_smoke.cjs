// Run through _serve_comfyui_image_container.py; never point this at a normal profile.
// Real Cat Chat uploads + task submission, local ComfyUI stub, desktop and 375px.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { chromium } = require('playwright');

const manifest = process.env.NAIBA_IMAGE_CONTAINER_FIXTURE;
assert.ok(manifest, 'Start verify/_serve_comfyui_image_container.py first (isolated fixture required)');
const fixture = JSON.parse(fs.readFileSync(manifest, 'utf8'));
const BASE = fixture.base;
const SLOT = fixture.slot;
const errors = [];
const uploads = [];
const submitted = [];

async function state(request) {
  const response = await request.get(`${BASE}/_fixture/image-container`);
  assert.equal(response.status(), 200, 'Only the isolated ImageContainer service may be used');
  const payload = await response.json();
  assert.equal(payload.isolated, true);
  return payload;
}

async function waitUntil(read, accept, description, timeout = 15000) {
  const end = Date.now() + timeout;
  let latest;
  do {
    latest = await read();
    if (accept(latest)) return latest;
    await new Promise(resolve => setTimeout(resolve, 100));
  } while (Date.now() < end);
  assert.fail(`${description}: ${JSON.stringify(latest)}`);
}

async function open(page) {
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.locator('#openStudio').click();
  await page.locator('[data-studio-page="comfyui"]').click();
  await page.locator('[data-comfyui-pane="workflows"]').click();
  await page.locator('.comfyui-workflow-title').filter({ hasText: fixture.workflow_name }).click();
  await page.locator('[data-comfyui-pane="run"]').click();
  await page.locator(`[data-image-container] input[type="hidden"][data-comfyui-slot="${SLOT}"]`).waitFor({ state: 'attached' });
}

async function readRows(page, container) {
  const hidden = container.locator('input[type="hidden"][data-comfyui-slot]');
  if (await hidden.count()) return JSON.parse(await hidden.inputValue());
  await container.getByRole('button', { name: '编辑原始 JSON', exact: true }).click();
  await page.locator('#comfyuiJsonDialog[open]').waitFor();
  const rows = JSON.parse(await page.locator('#comfyuiJsonRaw').inputValue());
  await page.locator('#comfyuiJsonCancel').click();
  return rows;
}

async function upload(page, container, index, image) {
  const responsePromise = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/comfyui/media' && response.request().method() === 'POST');
  await container.locator('input[type="file"]').nth(index).setInputFiles(image);
  const response = await responsePromise;
  assert.equal(response.status(), 200, await response.text());
  const picked = await response.json();
  assert.ok(picked.path && picked.name && picked.thumb_path, 'Real upload returns managed image and thumbnail paths');
  assert.ok(picked.path.startsWith(fixture.runtime), 'Upload stays inside the isolated runtime');
  await waitUntil(() => container.getByRole('button', { name: '清空选择', exact: true }).isEnabled(), Boolean, 'Upload controls become available');
  await container.locator('img').first().waitFor();
  return picked;
}

async function geometry(page, container, label) {
  const result = await container.evaluate(element => {
    const bounds = element.getBoundingClientRect();
    const outside = Array.from(element.querySelectorAll('button,input,img,.comfyui-media-tile')).filter(node => {
      if (!node.getClientRects().length) return false;
      const rect = node.getBoundingClientRect();
      return rect.left < bounds.left - 2 || rect.right > bounds.right + 2;
    }).map(node => ({ tag: node.tagName, text: node.getAttribute('aria-label') || node.textContent }));
    return { outside, client: element.clientWidth, scroll: element.scrollWidth,
      width: window.innerWidth, document: document.documentElement.scrollWidth };
  });
  assert.deepEqual(result.outside, [], `${label}: container controls fit`);
  assert.ok(result.scroll <= result.client + 2, `${label}: container has no horizontal overflow: ${JSON.stringify(result)}`);
  assert.ok(result.document <= result.width + 2, `${label}: page fits viewport: ${JSON.stringify(result)}`);
}

async function exercise(page, container, label) {
  assert.equal(await container.getByRole('button', { name: '编辑原始 JSON', exact: true }).count(), 1);
  assert.deepEqual(await readRows(page, container), [], `${label}: starts empty`);
  const first = await upload(page, container, 0, fixture.images[0]);
  const second = await upload(page, container, 1, fixture.images[1]);
  let rows = await readRows(page, container);
  assert.deepEqual(rows.map(row => row._cat_chat_media.path), [first.path, second.path], `${label}: selections keep order`);
  for (const row of rows) {
    assert.deepEqual(Object.keys(row).sort(), ['_cat_chat_media', 'name', 'subfolder', 'type']);
    assert.equal(row.subfolder, '');
    assert.equal(row.type, 'input');
    assert.equal(row.name, row._cat_chat_media.name);
  }
  await container.getByRole('button', { name: '图片 2 上移', exact: true }).click();
  assert.deepEqual((await readRows(page, container)).map(row => row._cat_chat_media.path), [second.path, first.path], `${label}: move up`);
  await container.getByRole('button', { name: '图片 1 下移', exact: true }).click();
  assert.deepEqual((await readRows(page, container)).map(row => row._cat_chat_media.path), [first.path, second.path], `${label}: move down`);
  const replacement = await upload(page, container, 0, fixture.images[2]);
  assert.deepEqual((await readRows(page, container)).map(row => row._cat_chat_media.path), [replacement.path, second.path], `${label}: replace one image`);
  await container.locator('.comfyui-media-tile').first().hover();
  await container.getByRole('button', { name: '清除选择，这个输出不选图片', exact: true }).first().click();
  assert.deepEqual((await readRows(page, container)).map(row => row._cat_chat_media.path), [second.path], `${label}: clear one shifts later output`);
  await container.getByRole('button', { name: '清空选择', exact: true }).click();
  assert.deepEqual(await readRows(page, container), [], `${label}: clear selection writes []`);
  await upload(page, container, 0, fixture.images[0]);
  await upload(page, container, 1, fixture.images[1]);
  rows = await readRows(page, container);
  await container.getByRole('button', { name: '编辑原始 JSON', exact: true }).click();
  await page.locator('#comfyuiJsonRaw').fill(JSON.stringify([...rows].reverse()));
  await page.locator('#comfyuiJsonRaw').press('Tab');
  await page.locator('#comfyuiJsonApply').click();
  await page.locator('#comfyuiJsonDialog').waitFor({ state: 'hidden' });
  const applied = await readRows(page, container);
  assert.deepEqual(applied, [...rows].reverse(), `${label}: advanced JSON apply updates inline selection`);
  await container.getByRole('button', { name: '编辑原始 JSON', exact: true }).click();
  await page.locator('#comfyuiJsonRaw').fill('[]');
  await page.locator('#comfyuiJsonRaw').press('Tab');
  await page.locator('#comfyuiJsonCancel').click();
  assert.deepEqual(await readRows(page, container), applied, `${label}: raw JSON cancel leaves selection untouched`);
  await geometry(page, container, label);
  await container.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(fixture.output, `${label}.png`) });
  console.log(`PASS ${label}: upload/select/replace/reorder/clear/[]/raw JSON apply/cancel/layout`);
  return applied;
}

async function awaitPrompts(request, before, count) {
  const payload = await waitUntil(() => state(request), next => next.prompts.length >= before + count, 'Stub receives submitted prompt', 30000);
  return payload.prompts.slice(before, before + count);
}

async function verifyOutput(request, prompts, expected, label) {
  const stub = await state(request);
  const uploaded = new Map(stub.uploads.map(item => [item.name, item.sha256]));
  assert.equal(prompts.length, expected.length);
  prompts.forEach((item, index) => {
    const text = item.prompt['1'].inputs.selected_json;
    assert.equal(typeof text, 'string');
    const rows = JSON.parse(text);
    assert.equal(rows.length, expected[index].length, `${label}: selected output count`);
    for (const [rowIndex, row] of rows.entries()) {
      const digest = createHash('sha256').update(fs.readFileSync(expected[index][rowIndex]._cat_chat_media.path)).digest('hex');
      assert.equal(uploaded.get(row.name), digest, `${label}: output ${rowIndex + 1} retains selected image and order`);
      assert.equal(row.type, 'input');
      assert.equal(row.subfolder, 'smoke-input');
      assert.match(row.name, /^uploaded-\d+\.png$/);
      assert.equal(Object.hasOwn(row, '_cat_chat_media'), false, `${label}: private host media data removed before ComfyUI`);
    }
    assert.equal(item.prompt['1'].inputs.pool_json, '[]', `${label}: pool_json remains opaque`);
  });
}

async function nativeDefaultClear(page, width) {
  const native = [{ name: 'existing-native.png', subfolder: 'native-input', type: 'input' }];
  const name = `原生默认图清空验收-${width}`;
  const response = await page.request.post(`${BASE}/api/comfyui/workflows`, { data: {
    name, workflow: {
      '1': { class_type: 'ImageContainer', inputs: { selected_json: JSON.stringify(native), pool_json: '[]' } },
      '2': { class_type: 'SaveImage', inputs: { images: ['1', 0], filename_prefix: 'native-clear-smoke' } },
    },
  } });
  assert.ok(response.ok(), await response.text());
  await page.locator('[data-comfyui-pane="workflows"]').click();
  await page.locator('#comfyuiRefreshWorkflows').click();
  await page.locator('.comfyui-workflow-title').filter({ hasText: name }).click();
  await page.locator('[data-comfyui-pane="run"]').click();
  const container = page.locator('[data-image-container]').filter({ has: page.locator(`input[data-comfyui-slot="${SLOT}"]`) });
  await container.waitFor();
  assert.deepEqual(await readRows(page, container), native, 'Imported ImageContainer is visible by default with its native reference');
  await container.locator('.comfyui-media-tile').first().hover();
  await container.getByRole('button', { name: '清除选择，这个输出不选图片', exact: true }).click();
  assert.deepEqual(await readRows(page, container), [], 'Clearing native default writes [] instead of restoring it');
  const before = (await state(page.request)).prompts.length;
  await page.locator('#comfyuiSubmit').click();
  await verifyOutput(page.request, await awaitPrompts(page.request, before, 1), [[]], `${width}-native-clear`);
  await geometry(page, container, `${width}-native-clear`);
  await page.screenshot({ path: path.join(fixture.output, `${width}-native-clear.png`) });
  console.log(`PASS ${width}-native-clear: default visible/native clear writes []/real submission`);
}

async function viewport(browser, width) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
  page.setDefaultTimeout(10000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() !== 'error') return;
    const location = message.location().url;
    if (location && new URL(location).pathname === '/favicon.ico') return;
    errors.push(`${message.text()} ${location || ''}`.trim());
  });
  page.on('response', response => {
    const url = new URL(response.url());
    if (url.pathname === '/api/comfyui/media' && response.request().method() === 'POST') uploads.push(response.status());
    if (response.status() >= 400 && url.pathname !== '/favicon.ico') errors.push(`HTTP ${response.status()} ${url.pathname}`);
  });
  page.on('request', request => {
    if (request.method() === 'POST' && ['/api/comfyui/tasks', '/api/comfyui/batches'].includes(new URL(request.url()).pathname)) {
      submitted.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() });
    }
  });
  page.on('dialog', dialog => dialog.accept());
  try {
    await open(page);
    await page.setViewportSize({ width, height: width === 375 ? 844 : 960 });
    const single = page.locator('[data-image-container]').filter({ has: page.locator(`input[data-comfyui-slot="${SLOT}"]`) });
    const selected = await exercise(page, single, `${width}-single`);
    let before = (await state(page.request)).prompts.length;
    await page.locator('#comfyuiSubmit').click();
    await verifyOutput(page.request, await awaitPrompts(page.request, before, 1), [selected], `${width}-single`);
    await nativeDefaultClear(page, width);

    await page.locator('[data-comfyui-pane="batch"]').click();
    await page.locator('[data-batch-select="workflow"]').selectOption(fixture.workflow_id);
    await page.locator('[data-batch-action="toggle-fixed"]').click();
    const fixed = page.locator(`[data-image-container-source="fixed"][data-json-key="${SLOT}"] [data-image-container]`);
    await fixed.waitFor();
    const fixedRows = await exercise(page, fixed, `${width}-fixed`);
    await page.locator('[data-batch-item="name"]').fill('固定参数图片验收');
    before = (await state(page.request)).prompts.length;
    await page.locator('[data-batch-action="submit"]').click();
    await verifyOutput(page.request, await awaitPrompts(page.request, before, 1), [fixedRows], `${width}-fixed`);

    // Return to the same draft; identify the container as a per-item extra and add another row.
    await page.locator('[data-batch-action="back-to-draft"]').click();
    if (!(await fixed.isVisible())) await page.locator('[data-batch-action="toggle-fixed"]').click();
    await fixed.getByRole('button', { name: '清空选择', exact: true }).click();
    await page.locator('[data-batch-action="add-extra"]').click();
    await page.locator('[data-batch-extra-slot]').selectOption(SLOT);
    await page.locator('[data-batch-action="add-item"]').click();
    await page.locator('[data-batch-item="name"]').nth(1).fill('空选择图片验收');
    const extras = page.locator('[data-image-container-source="extras"] [data-image-container]');
    await waitUntil(() => extras.count(), count => count === 2, 'Two independent extras containers');
    const extraRows = await exercise(page, extras.nth(0), `${width}-extras`);
    assert.deepEqual(await readRows(page, extras.nth(1)), [], `${width}: per-item extras remain independent`);
    before = (await state(page.request)).prompts.length;
    await page.locator('[data-batch-action="submit"]').click();
    await verifyOutput(page.request, await awaitPrompts(page.request, before, 2), [extraRows, []], `${width}-extras`);
    const expectedTaskCount = width === 1440 ? 5 : 10;
    await waitUntil(async () => {
      const response = await page.request.get(`${BASE}/api/comfyui/tasks`);
      assert.equal(response.status(), 200);
      return (await response.json()).tasks;
    }, tasks => tasks.length === expectedTaskCount && tasks.every(task => task.status === 'completed'),
    'Every real Cat Chat task finishes and downloads the PNG output', 30000);
    assert.deepEqual(errors, [], 'No browser errors or failed HTTP responses');
  } catch (error) {
    await page.screenshot({ path: path.join(fixture.output, `${width}-failed.png`) });
    fs.writeFileSync(path.join(fixture.output, `${width}-failed.html`), await page.content());
    throw error;
  } finally {
    await page.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const request = await browser.newContext();
    await state(request.request);
    await request.close();
    for (const width of [1440, 375]) await viewport(browser, width);
    assert.ok(uploads.length >= 30 && uploads.every(status => status === 200), 'Every selection uses real successful Cat Chat upload');
    assert.ok(submitted.length >= 8, 'Single and batch runs use real Cat Chat submission APIs');
    console.log(`PASS desktop/375 ImageContainer: ${uploads.length} real image uploads, ${submitted.length} submissions`);
  } finally {
    fs.writeFileSync(path.join(fixture.output, 'browser-results.json'), JSON.stringify({ errors, uploads, submitted }, null, 2));
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
