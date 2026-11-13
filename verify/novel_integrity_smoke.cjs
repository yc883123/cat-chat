// Real HTTP and browser checks; source.json must originate from this run's isolated harness.
const { chromium } = require('playwright');
const strict = require('node:assert/strict');
let assertions = 0;
function assert(...args) { assertions += 1; return strict(...args); }
for (const name of ['equal', 'deepEqual']) {
  assert[name] = (...args) => { assertions += 1; return strict[name](...args); };
}
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const fixturePath = process.env.NAIBA_NOVEL_SOURCE_FILE;
assert(fixturePath, 'isolated source fixture is required');
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(fixture.base));
const output = path.resolve(process.env.NAIBA_SMOKE_OUTPUT || path.dirname(fixturePath));
const normalize = value => path.resolve(value).toLowerCase();

function fingerprints(root) {
  const rows = {};
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const file = path.join(root, entry.name);
    if (entry.isDirectory()) {
      for (const [name, digest] of Object.entries(fingerprints(file))) rows[path.join(entry.name, name)] = digest;
    } else rows[entry.name] = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  }
  return rows;
}

async function json(context, url, data) {
  const response = data === undefined ? await context.request.get(fixture.base + url)
    : await context.request.post(fixture.base + url, { data });
  assert.equal(response.status(), 200, `${url}: ${await response.text()}`);
  return response.json();
}

async function enter(page, domain, bookId) {
  await page.evaluate(async ({ domain, bookId }) => {
    const studio = await import('/js/22-studio.js');
    studio.openStudioView(); studio.setStudioPage(domain);
    if (bookId) window.dispatchEvent(new CustomEvent('naiba-open-domain', { detail: { domain, bookId } }));
  }, { domain, bookId });
}

async function unknown(context, page, source, width) {
  const novelRoot = path.join(fixture.runtime, 'data', 'studio', 'novel');
  const before = fingerprints(novelRoot);
  const selection = { book_id: source.id, source_kind: 'manuscript', end_chapter_no: 3,
    manuscripts_by_chapter: source.manuscripts };
  const preview = await json(context, '/api/writer/novel-import/preview', selection);
  assert.equal(preview.ready, true);
  assert.equal(preview.coverage.comparison, 'unknown');
  assert.equal(preview.coverage.divergent_chapters, null);
  assert.deepEqual(Object.fromEntries(preview.selected_chapters.map(row => [row.no, row.manuscript_id])), source.manuscripts);
  assert.deepEqual(fingerprints(novelRoot), before, 'preflight modified the source data');

  await enter(page, 'writer');
  await page.locator('#writerBookMenuBtn').click();
  await page.locator('#writerNovelContinueBtn').click();
  await page.waitForFunction(() => !document.querySelector('#writerNovelImportNext').disabled);
  await page.locator('[data-writer-source-mode="book"]').click();
  await page.waitForFunction(() => !document.querySelector('#writerNovelSourceBook')?.disabled
    && document.querySelector('#writerNovelSourceBook')?.options.length > 1);
  await page.locator('#writerNovelSourceBook').selectOption(source.id);
  await page.locator('#writerNovelImportNext').click();
  await page.locator('input[name="writerNovelSourceKind"][value="manuscript"]').check();
  await page.locator('#writerNovelImportNext').click();
  for (const [no, id] of Object.entries(source.manuscripts)) {
    await page.locator(`[data-novel-source-chapter="${no}"]`).selectOption(id);
  }
  await page.locator('#writerNovelImportNext').click();
  const note = page.locator('#writerNovelImportBody .writer-note').filter({ hasText: '与原文的差异' });
  await note.waitFor();
  assert((await note.innerText()).includes('与原文的差异尚无法完整核对'));
  assert(!(await note.innerText()).includes('与原文内容不同的章节 0 章'));
  await note.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, `unknown_${source.damage}_${width}.png`) });
  const overflow = await page.locator('#writerNovelImportDialog').evaluate(dialog => {
    const bounds = dialog.getBoundingClientRect();
    return [...dialog.querySelectorAll('*')].filter(node => {
      if (!node.getClientRects().length || node.tagName === 'OPTION') return false;
      const rect = node.getBoundingClientRect();
      return rect.width > 0 && (rect.right > bounds.right + 1 || rect.left < bounds.left - 1);
    }).map(node => node.id || node.className || node.tagName);
  });
  assert.deepEqual(overflow, [], 'unknown note exceeds dialog bounds');
  await page.locator('#writerNovelImportClose').click();
  assert.deepEqual(fingerprints(novelRoot), before, 'viewing source preflight modified original manuscripts');
  console.log(`通过 ${width}px：原文 ${source.damage} 真实 HTTP unknown/null、选稿不变、界面提示、来源只读`);
}

async function reorder(context, page, width) {
  const id = fixture.script.id;
  const detailBefore = (await json(context, `/api/novel/books/${id}`)).book;
  const older = detailBefore.script_versions.slice(0, -1);
  assert.equal(detailBefore.script.length, 3);
  const expected = [detailBefore.script[2], detailBefore.script[0], detailBefore.script[1]]
    .map((row, index) => ({ ...row, i: index + 1 }));
  await enter(page, 'novel', id);
  await page.locator('[data-novel-stage="script"]').click();
  if (width < 761) await page.locator('.novel-pane-btn[data-novel-pane="editor"]').click();
  await page.waitForFunction(() => document.querySelectorAll('#novelSceneList .novel-scene-row').length === 3);
  assert(await page.locator('#novelSceneList').evaluate(node => node.clientHeight > 0), 'scene list collapsed to zero height');
  await page.screenshot({ path: path.join(output, `scene_before_${width}.png`) });
  for (const no of [3, 2]) {
    const button = page.locator(`#novelSceneList [data-novel-scene-action="up"][data-novel-scene-no="${no}"]`);
    await button.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `scene_action_${width}_${no}.png`) });
    const [response] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith(`/api/novel/books/${id}/script/reorder`), { timeout: 10000 }),
      button.click({ timeout: 5000 }),
    ]);
    assert.equal(response.status(), 200);
    await page.waitForFunction(target => {
      const list = document.querySelectorAll('#novelSceneList .novel-scene-open strong');
      return list[target.index]?.textContent === target.scene;
    }, { index: no - 2, scene: expected[0].scene });
  }
  const detail = (await json(context, `/api/novel/books/${id}`)).book;
  const listed = await json(context, `/api/novel/books/${id}/script`);
  assert.deepEqual(detail.script, expected);
  assert.deepEqual(listed.script, expected);
  assert.deepEqual(detail.script_versions.at(-1).script, expected);
  assert.deepEqual(detail.script_versions.slice(0, -1), older);
  await page.reload({ waitUntil: 'networkidle' });
  await enter(page, 'novel', id);
  await page.locator('[data-novel-stage="script"]').click();
  if (width < 761) await page.locator('.novel-pane-btn[data-novel-pane="editor"]').click();
  await page.waitForFunction(expectedTitles => {
    const titles = [...document.querySelectorAll('#novelSceneList .novel-scene-open strong')].map(node => node.textContent);
    return JSON.stringify(titles) === JSON.stringify(expectedTitles);
  }, expected.map(row => row.scene));
  assert.deepEqual(await page.locator('#novelSceneList .novel-scene-open small').allTextContents(),
    expected.map(row => `第 ${row.from_chapter} 章 · ${row.content.length} 字`));
  assert(await page.locator('#novelSceneList').evaluate(node => node.scrollWidth <= node.clientWidth + 1), 'scene list overflows horizontally');
  await page.locator('#novelSceneBlock').scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, `scene_reorder_reload_${width}.png`) });
  console.log(`通过 ${width}px：真实场次按钮排序、GET/最新版本一致、历史版本不变、页面重载顺序/来源保留`);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const viewport of [{ width: 1280, height: 900 }, { width: 375, height: 812 }]) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('console', message => {
        if (message.type() !== 'error') return;
        const url = message.location().url || '';
        if (url === fixture.base + '/favicon.ico' && message.text().includes('404')) return;
        errors.push(`${message.text()} ${url}`);
      });
      const bootstrap = await json(context, '/api/bootstrap');
      assert.equal(normalize(bootstrap.data_location.data_dir), normalize(path.join(fixture.runtime, 'data')));
      assert.equal(bootstrap.data_location.is_frozen, false);
      assert.equal((bootstrap.providers || []).length, 0);
      assert.equal((bootstrap.mcp_servers || []).length, 0);
      await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
      await page.goto(fixture.base, { waitUntil: 'networkidle' });
      assert.equal(await page.title(), 'Cat Chat');
      for (const source of fixture.damaged) await unknown(context, page, source, viewport.width);
      await reorder(context, page, viewport.width);
      assert.deepEqual(errors, [], 'browser errors');
      await context.close();
    }
    console.log(`novel integrity: ${assertions} assertions passed`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
