// 真实源码服务 + 测试小说：五步选稿、新章落点、只读来源和断网后幂等重试。
// 必须提供本轮隔离服务生成的 NAIBA_NOVEL_SOURCE_FILE，不接受常驻用户实例。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const fixturePath = process.env.NAIBA_NOVEL_SOURCE_FILE;
assert(fixturePath, '必须指定隔离服务来源文件');
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const BASE = fixture.base;
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(BASE), '只连接本轮回环服务');
const OUTPUT = path.resolve(process.env.NAIBA_SMOKE_OUTPUT || path.dirname(fixturePath));
fs.mkdirSync(OUTPUT, { recursive: true });
const normalizePath = file => path.resolve(file).toLowerCase();

async function run(browser, viewport, sourceKind, source, lostResponse) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const failures = [], committed = [];
  let firstSubmission = null, loseNext = lostResponse;
  page.on('pageerror', error => failures.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) failures.push(message.text());
  });
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  const bootstrap = await (await context.request.get(BASE + '/api/bootstrap')).json();
  assert.equal(normalizePath(bootstrap.data_location.data_dir), normalizePath(path.join(fixture.runtime, 'data')));
  assert.equal(bootstrap.data_location.is_frozen, false);
  assert(!(bootstrap.providers || []).length && !(bootstrap.mcp_servers || []).length);
  const sourceBefore = [];
  for (const no of [1, 2, 3]) sourceBefore.push(await (await context.request.get(`${BASE}/api/novel/books/${source.id}/chapters/${no}`)).json());
  const beforeCount = (await (await context.request.get(BASE + '/api/writer/books')).json()).books.length;
  await page.route('**/api/writer/novel-import', async route => {
    const request = route.request().postDataJSON();
    committed.push(request);
    if (loseNext) {
      loseNext = false;
      const response = await route.fetch();
      assert.equal(response.status(), 201);
      firstSubmission = await response.json();
      await route.abort('failed');
    } else await route.continue();
  });
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.evaluate(async () => {
    const studio = await import('/js/22-studio.js');
    studio.openStudioView(); studio.setStudioPage('writer');
  });
  await page.locator('#writerNovelContinueBtn').click();
  await page.locator('#writerNovelImportNext').waitFor({ state: 'visible' });
  await page.waitForFunction(() => !document.querySelector('#writerNovelImportNext').disabled);
  await page.locator('#writerNovelSourceBook').selectOption(source.id);
  await page.locator('#writerNovelImportNext').click();
  await page.locator('input[name="writerNovelSourceKind"]').first().waitFor();
  await page.locator('#writerNovelImportNext').click();
  await page.waitForFunction(() => document.querySelector('#writerNovelImportError').textContent.includes('明确选择'));
  await page.locator(`input[name="writerNovelSourceKind"][value="${sourceKind}"]`).check();
  await page.locator('#writerNovelImportNext').click();
  const endNo = sourceKind === 'original' ? 3 : 2;
  await page.locator('#writerNovelEndChapter').selectOption(String(endNo));
  if (sourceKind === 'manuscript') {
    await page.locator('#writerNovelImportNext').click();
    await page.waitForFunction(() => document.querySelector('#writerNovelImportError').textContent.trim().length > 0);
    for (let no = 1; no <= endNo; no++) {
      await page.locator(`[data-novel-source-chapter="${no}"]`).selectOption(source.manuscripts[no]);
    }
  }
  await page.locator('#writerNovelImportNext').click();
  await page.locator('[data-continuation-summary="characters_state"]').waitFor();
  assert((await page.locator('#writerNovelImportBody').innerText()).includes(`第 ${endNo} 章`));
  for (const [key, text] of Object.entries({
    characters_state: '姜乐在旧港，陆行舟在船上。', relations_state: '二人继续合作，尚未互相坦白。',
    plot_state: '旧船照片的日期尚未解释。', next_outline: '新章从照片背面的日期开始追查。',
  })) await page.locator(`[data-continuation-summary="${key}"]`).fill(text);
  await page.locator('#writerNovelImportNext').click();
  await page.locator('#writerNovelImportNext').click();
  await page.waitForFunction(() => document.querySelector('#writerNovelImportError').textContent.includes('请先确认'));
  await page.locator('#writerNovelImportConfirmed').check();
  await page.screenshot({ path: path.join(OUTPUT, `writer_import_confirm_${viewport.width}.png`) });
  const outside = await page.locator('#writerNovelImportDialog').evaluate(dialog => {
    const bounds = dialog.getBoundingClientRect();
    return [...dialog.querySelectorAll('*')].filter(node => {
      if (!node.getClientRects().length || ['OPTION'].includes(node.tagName)) return false;
      const rect = node.getBoundingClientRect();
      return rect.width > 0 && (rect.right > bounds.right + 1 || rect.left < bounds.left - 1);
    }).map(node => node.id || node.className || node.tagName);
  });
  assert.deepEqual(outside, [], '向导控件必须落在对话框边界内');
  await page.locator('#writerNovelImportNext').click();
  if (lostResponse) {
    await page.waitForFunction(() => !document.querySelector('#writerNovelImportNext').disabled
      && document.querySelector('#writerNovelImportError').textContent.length > 0);
    assert(firstSubmission?.book?.id);
    await page.locator('#writerNovelImportNext').click();
  }
  await page.waitForFunction(() => !document.querySelector('#writerNovelImportDialog').open);
  await page.waitForFunction(no => document.querySelector('#writerDraftTitle').textContent.startsWith(`第${no}章`), endNo + 1);
  const latest = (await (await context.request.get(BASE + '/api/writer/books')).json()).books;
  assert.equal(latest.length, beforeCount + 1);
  const request = committed.at(-1);
  const duplicate = await context.request.post(BASE + '/api/writer/novel-import', { data: request });
  assert.equal(duplicate.status(), 200);
  const result = await duplicate.json();
  assert.equal(result.duplicate, true);
  assert.equal(result.new_chapter_no, endNo + 1);
  if (lostResponse) {
    assert.equal(committed[0].request_key, committed[1].request_key);
    assert.equal(result.book.id, firstSubmission.book.id);
  }
  const bookId = result.book.id;
  const detail = await (await context.request.get(`${BASE}/api/writer/books/${bookId}`)).json();
  assert.equal(detail.chapters.length, endNo + 1);
  for (let no = 1; no <= endNo; no++) {
    const chapter = await (await context.request.get(`${BASE}/api/writer/books/${bookId}/chapters/${no}`)).json();
    assert.equal(chapter.read_only, true);
    assert.equal(chapter.source.source_kind, sourceKind);
    assert.equal(chapter.source.source_chapter_no, no);
    assert.equal(chapter.source.source_book_id, source.id);
    if (sourceKind === 'manuscript') assert.equal(chapter.source.source_manuscript_id, source.manuscripts[no]);
    const denied = await context.request.patch(`${BASE}/api/writer/books/${bookId}/chapters/${no}`, { data: { content: '禁止覆盖旧章', revision: chapter.revision } });
    assert.equal(denied.status(), 400);
  }
  if (viewport.width < 761) await page.locator('[data-writer-pane="book"]').click();
  await page.locator('#writerChapterList [data-writer-chapter="1"]').click();
  if (viewport.width < 761) await page.locator('[data-writer-pane="editor"]').click();
  await page.waitForFunction(() => document.querySelector('#writerDraftContent').readOnly);
  assert.equal(await page.locator('#writerDraftContinue').isDisabled(), true);
  assert.equal(await page.locator('#writerDraftGenerate').isDisabled(), true);
  assert((await page.locator('#writerImportedSourceNote').innerText()).includes('只读'));
  await page.screenshot({ path: path.join(OUTPUT, `writer_import_readonly_${viewport.width}.png`) });
  for (let no = 1; no <= 3; no++) {
    const after = await (await context.request.get(`${BASE}/api/novel/books/${source.id}/chapters/${no}`)).json();
    assert.deepEqual(after, sourceBefore[no - 1], '导入不改写拆书匠章节和稿件版本');
  }
  assert.deepEqual(failures, []);
  console.log(`通过 ${viewport.width}×${viewport.height}：${sourceKind === 'original' ? '原文' : '逐章改编稿'}、末章+1、人工确认、只读来源、原作不变、${lostResponse ? '丢失回包后幂等重试' : '重复确认不重复建书'}`);
  await context.close();
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    await run(browser, { width: 1280, height: 900 }, 'original', fixture.books[0], true);
    await run(browser, { width: 375, height: 812 }, 'manuscript', fixture.books[1], false);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
