// Real product modules and IndexedDB; all novel/writer network calls are local fixtures.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8827';
const OUTPUT = process.env.NAIBA_SMOKE_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'naiba-bodydraft-shots-'));
fs.mkdirSync(OUTPUT, { recursive: true });
const NID = 'draft-novel', WID = 'draft-writer';
const initial = '门外有人敲了三下。\n她没有开门。';
const writer = new Map([1, 2].map(no => [no, { no, title: `第${no}章`, content: initial + no, revision: 1, current_version_id: `v${no}-1`, versions: [] }]));
let novelRevision = 1, writerSaves = 0, novelSaves = 0, holdSave = null, missingOriginal = false;
let activePipeline = null;
const novelRows = no => [{ id: `o${no}`, kind: 'original', label: '原文', chapter_no: no, len: initial.length },
  { id: `a${no}`, kind: 'manual', label: '人工改编稿', chapter_no: no, len: initial.length }]
  .filter(row => !missingOriginal || row.id !== 'o1');
const novelChapter = no => ({ i: no, title: `第${no}章`, content: initial + no, len: initial.length,
  revision: novelRevision, manuscripts: novelRows(no), base_manuscript_id: missingOriginal && no === 1 ? 'a1' : `o${no}`, content_manuscript_id: missingOriginal && no === 1 ? 'a1' : `o${no}` });
const book = id => ({ id, title: id === WID ? '正文恢复测试作品' : '稿件恢复测试作品', revision: 1,
  chapter_count: 2, current_setup_version_id: 'su1', current_outline_version_id: 'ol1', active_pipeline: id === WID ? activePipeline : null,
  setup_versions: [], outline_versions: [], elements_versions: [], script_versions: [] });
const setup = { title: '正文恢复测试作品', genre: {}, characters: [], constraints: {} };
async function fixture(page, failStorage = false) {
  await page.addInitScript(({ WID, NID, failStorage }) => {
    localStorage.setItem('naibaOnboardingDismissed', '1');
    localStorage.setItem('naibaWriterLastBookId', WID); localStorage.setItem('naibaNovelLastBookId', NID);
    if (failStorage) Object.defineProperty(window, 'indexedDB', { value: undefined });
  }, { WID, NID, failStorage });
  const json = (route, data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
  await page.route('**/api/providers/models', route => json(route, { models: [] }));
  await page.route('**/api/studio/tasks**', route => {
    const p = new URL(route.request().url()).pathname;
    if (p.endsWith('/events')) return json(route, { events: [] });
    if (p.endsWith('/draft-job')) return json(route, { task: { id: 'draft-job', status: activePipeline?.status === 'cancelled' ? 'cancelled' : 'running' } });
    return json(route, { tasks: [] });
  });
  await page.route('**/api/writer/**', async route => {
    const p = new URL(route.request().url()).pathname;
    const no = Number(p.match(/chapters\/(\d+)/)?.[1]);
    if (no && route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON(); writerSaves++;
      if (holdSave) await holdSave();
      const current = writer.get(no);
      if (body.revision !== current.revision) return json(route, { error: '版本冲突' }, 409);
      current.content = body.content; current.revision++; current.current_version_id = `v${no}-${current.revision}`;
      return json(route, { chapter_revision: current.revision });
    }
    if (no) return json(route, writer.get(no));
    if (p === '/api/writer/books') return json(route, { books: [book(WID)], corrupt: [] });
    if (p === `/api/writer/books/${WID}`) return json(route, { book: book(WID), setup,
      outline: { id: 'ol1', version: 1, parts: [{ no: 1, title: '开端', text: '故事开始' }], chapters: [...writer.values()] },
      chapters: [...writer.values()], broken: [] });
    if (p.endsWith('/pipeline/cancel')) {
      activePipeline = { ...activePipeline, status: 'stopping', error: '停止尚未确认；已阻止后续步骤' };
      return json(route, { pipeline: activePipeline });
    }
    if (p.endsWith('/pipeline')) return json(route, { pipeline: activePipeline });
    if (p.includes('/preview')) return json(route, { budget: { fits: true, included: [], omitted: [], suggestions: [] } });
    return json(route, {});
  });
  await page.route('**/api/novel/**', async route => {
    const p = new URL(route.request().url()).pathname;
    const no = Number(p.match(/chapters\/(\d+)/)?.[1]);
    if (no && route.request().method() === 'PATCH') {
      novelSaves++;
      if (holdSave) await holdSave();
      novelRevision++;
      return json(route, { chapter: { ...novelChapter(no), created_manuscript: { label: '人工改编稿', len: 10 } } });
    }
    if (p.includes('/manuscripts/')) return json(route, { content: '第二份稿件的正文' });
    if (no) return json(route, novelChapter(no));
    if (p === '/api/novel/books') return json(route, { books: [book(NID)], corrupt: [] });
    if (p === `/api/novel/books/${NID}`) return json(route, { book: book(NID), chapters: [novelChapter(1), novelChapter(2)], broken: [] });
    if (p.includes('/preview')) return json(route, { warnings: [], sources: {}, chapters: [], budget: { fits: true, suggestions: [] } });
    return json(route, {});
  });
}
const errors = [];
function listen(page) {
  page.on('pageerror', error => { errors.push(error.message); console.error('pageerror:', error.message); });
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
}
function clean() { assert.deepEqual(errors, [], 'page errors'); }
async function valueEquals(page, selector, text) {
  await page.waitForFunction(({ selector, text }) => document.querySelector(selector)?.value === text, { selector, text });
  assert.equal(await page.locator(selector).inputValue(), text);
  clean();
}
async function pane(page, module, name) {
  const button = page.locator(`button[data-${module}-pane="${name}"]`);
  if (await button.isVisible()) await button.click();
}
async function enter(page, module) {
  await page.evaluate(async module => { const studio = await import('/js/22-studio.js'); studio.openStudioView(); studio.setStudioPage(module); }, module);
  await page.waitForFunction(module => document.querySelector(`#${module}BookTitle`)?.textContent.includes('恢复测试'), module);
  clean();
}
async function writerChapterOpen(page, no) {
  await page.locator('[data-writer-stage="draft"]').click();
  await pane(page, 'writer', 'book');
  await page.locator(`[data-writer-chapter="${no}"]`).click();
  await pane(page, 'writer', 'editor');
  await page.waitForFunction(no => document.querySelector('#writerDraftTitle')?.textContent.startsWith(`第${no}章`), no);
  clean();
}
async function novelChapterOpen(page, no) {
  await pane(page, 'novel', 'chapters');
  await page.locator(`[data-novel-chapter="${no}"]`).click();
  await pane(page, 'novel', 'editor');
  await page.waitForSelector(`[data-novel-manuscript-card="o${no}"]`);
  clean();
}
async function stored(page, module, text) {
  await page.waitForFunction(async ({ module, text }) => {
    const rows = await new Promise((resolve, reject) => {
      const req = indexedDB.open('naibaBodyDrafts', 1);
      req.onerror = () => reject(req.error);
      req.onsuccess = () => { const db = req.result; const tx = db.transaction('drafts');
        tx.objectStore('drafts').getAll().onsuccess = event => resolve(event.target.result); tx.oncomplete = () => db.close(); };
    });
    return rows.some(row => row.module === module && row.content === text);
  }, { module, text });
  clean();
}
(async () => {
  let checks = 0;
  for (const width of [1280, 375]) {
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), `naiba-bodydraft-profile-${width}-`));
    let browser = await chromium.launchPersistentContext(profile, { channel: 'msedge', headless: true, viewport: { width, height: 900 } });
    try {
      let page = await browser.newPage(); listen(page); await fixture(page);
      await page.goto(BASE, { waitUntil: 'domcontentloaded' }); await enter(page, 'writer'); await writerChapterOpen(page, 1);
      const text = `织书台 ${width} 的未保存正文\n逐字恢复。`;
      await page.locator('#writerDraftContent').fill(text);
      await writerChapterOpen(page, 2); await writerChapterOpen(page, 1);
      await valueEquals(page, '#writerDraftContent', text); checks++;
      await stored(page, 'writer', text);
      await page.reload(); await enter(page, 'writer');
      await page.locator('[data-body-draft-list="writer"] button').first().click();
      await valueEquals(page, '#writerDraftContent', text); checks++;
      await pane(page, 'writer', 'editor');
      console.log(`writer ${width}: switch and refresh recovered`);
      await page.screenshot({ path: path.join(OUTPUT, `writer-recovered-${width}.png`) });
      const long = ('长正文，窗外的雨还在下。\n').repeat(6000);
      await page.locator('#writerDraftContent').fill('长正文输入检查');
      await page.locator('#writerDraftContent').evaluate((el, text) => { el.value = text; el.dispatchEvent(new Event('input', { bubbles: true })); }, long);
      await stored(page, 'writer', long); checks++;
      let release, submitted;
      const started = new Promise(resolve => { submitted = resolve; });
      holdSave = () => { submitted(); return new Promise(resolve => { release = resolve; }); };
      await page.locator('#writerDraftSave').click(); await started;
      const later = `保存期间继续输入 ${width}`;
      await page.locator('#writerDraftContent').fill(later); release(); holdSave = null;
      await page.waitForFunction(() => !document.querySelector('#writerDraftSave').disabled);
      await stored(page, 'writer', later);
      await valueEquals(page, '#writerDraftContent', later); checks++;
      await browser.close();
      browser = await chromium.launchPersistentContext(profile, { channel: 'msedge', headless: true, viewport: { width, height: 900 } });
      page = await browser.newPage(); listen(page); await fixture(page); await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await enter(page, 'writer'); await page.locator('[data-body-draft-list="writer"] button').first().click();
      await valueEquals(page, '#writerDraftContent', later); checks++;
      writer.get(1).revision++; writer.get(1).content = '服务器中的新版本';
      await page.reload(); await enter(page, 'writer'); await page.locator('[data-body-draft-list="writer"] button').first().click();
      await page.waitForSelector('[data-body-draft-conflict="writer"]');
      assert.equal(await page.locator('[data-body-draft-conflict="writer"] textarea').inputValue(), later);
      await page.locator('[data-body-draft-action="continue"]').click();
      await valueEquals(page, '#writerDraftContent', later); checks++;
      await stored(page, 'writer', later);
      await page.screenshot({ path: path.join(OUTPUT, `writer-draft-${width}.png`) });
      writer.get(2).read_only = true;
      await writerChapterOpen(page, 2);
      assert.equal(await page.locator('#writerDraftContent').evaluate(el => el.readOnly), true); checks++;
      writer.get(2).read_only = false;
      await enter(page, 'novel');
      await pane(page, 'novel', 'editor');
      await page.locator('[data-novel-manuscript-action="edit"]').first().click();
      const novelText = `拆书匠 ${width} 的本地改编正文`;
      await page.locator('#novelChapterBody').fill(novelText); await stored(page, 'novel', novelText);
      await novelChapterOpen(page, 2); await novelChapterOpen(page, 1);
      await valueEquals(page, '#novelChapterBody', novelText); checks++;
      await page.reload(); await enter(page, 'novel'); await pane(page, 'novel', 'editor');
      await page.waitForSelector('#novelChapterBody');
      await valueEquals(page, '#novelChapterBody', novelText); checks++;
      await page.screenshot({ path: path.join(OUTPUT, `novel-draft-${width}.png`) });
      await page.locator('[data-novel-manuscript-action="edit"][data-novel-manuscript-id="a1"]').click();
      await page.waitForFunction(() => document.querySelector('#novelManuscriptEditHead')?.textContent === '编辑人工改编稿');
      await page.locator('#novelChapterBody').fill('另一份稿件的独立草稿');
      await page.locator('[data-novel-manuscript-action="edit"][data-novel-manuscript-id="o1"]').click();
      await valueEquals(page, '#novelChapterBody', novelText); checks++;
      await page.locator('#novelChapterBody').fill(novelText + ' ');
      await page.locator('#novelChapterBody').fill(novelText);
      await stored(page, 'novel', novelText);
      missingOriginal = true;
      await page.reload(); await enter(page, 'novel');
      await page.waitForSelector('[data-body-draft-conflict="novel"]');
      assert.equal(await page.locator('[data-body-draft-conflict="novel"] textarea').inputValue(), novelText);
      await page.locator('[data-body-draft-action="keep"]').click();
      await stored(page, 'novel', novelText); clean(); checks++;
      missingOriginal = false;
      assert.equal(novelSaves, 0, 'local draft must not create manuscripts');
      activePipeline = { id: 'draft-pipeline', status: 'running', kind: 'chapter', current_task_id: 'draft-job', steps: ['chapter'], step_index: 0 };
      await enter(page, 'writer');
      await page.waitForSelector('#writerPipelineCancel', { state: 'attached' });
      await pane(page, 'writer', 'notes');
      await page.locator('#writerPipelineCancel').click();
      await page.waitForFunction(() => document.querySelector('#writerPipelineStatus')?.textContent === '停止尚未确认');
      assert.equal(await page.locator('#writerPipelineRetry').isVisible(), false);
      assert.equal(await page.locator('#writerDraftGenerate').isDisabled(), true);
      activePipeline = { ...activePipeline, status: 'cancelled', error: '' };
      await page.waitForFunction(() => document.querySelector('#writerPipelineStatus')?.textContent === '已取消');
      assert.equal(await page.locator('#writerPipelineRetry').isVisible(), true);
      clean(); checks++;
      activePipeline = null;
    } catch (error) {
      const page = browser.pages().at(-1);
      if (page) {
        await page.screenshot({ path: path.join(OUTPUT, `failure-${width}.png`) });
        console.log('failure diagnostic', OUTPUT, await page.locator('#writerDraftContent').evaluate(el => {
          const rows = []; for (let node = el; node; node = node.parentElement) rows.push({ id: node.id, hidden: node.hidden, inert: node.inert, disabled: node.disabled, readOnly: node.readOnly, display: getComputedStyle(node).display }); return rows;
        }));
      }
      throw error;
    } finally { await browser.close(); }
  }
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 375, height: 900 } }); listen(page); await fixture(page, true);
    await page.goto(BASE, { waitUntil: 'domcontentloaded' }); await enter(page, 'writer'); await writerChapterOpen(page, 1);
    await page.locator('#writerDraftContent').fill('存储失败仍保留正文');
    await page.waitForSelector('[data-body-draft-status="writer"][data-state="error"]');
    assert.equal(await page.locator('#writerDraftContent').inputValue(), '存储失败仍保留正文'); clean(); checks++;
    await page.screenshot({ path: path.join(OUTPUT, 'draft-storage-failure-375.png') });
    await enter(page, 'novel'); await pane(page, 'novel', 'editor');
    await page.locator('[data-novel-manuscript-action="edit"]').first().click();
    await page.locator('#novelChapterBody').fill('拆书匠存储失败仍保留正文');
    await page.waitForSelector('[data-body-draft-status="novel"][data-state="error"]');
    await valueEquals(page, '#novelChapterBody', '拆书匠存储失败仍保留正文'); checks++;
  } finally { await browser.close(); }
  console.log(JSON.stringify({ checks, writerSaves, novelSaves, screenshots: OUTPUT, errors }));
})().catch(error => { console.error(String(error).slice(0, 1800)); console.error(error.stack?.split('\n').slice(-5).join('\n')); process.exitCode = 1; });
