// 阶段面板截图：要素阶段的逐章选稿 + 双滑条 + 最近任务，桌面与手机各一张。
// 只截图，供人工/视觉验收看排版；业务断言在 novel_ui_smoke.cjs。
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8892';
const SHOT_DIR = process.env.NAIBA_SHOT_DIR || path.join(__dirname, '_tmp_novel_shots');
const BOOK_ID = 'book-20260101-abcdefabcdef';
const CHAPTERS = [
  { i: 1, title: '第1章 雨夜来客', len: 4860, manuscript_count: 3, adapted_count: 2,
    base_manuscript_id: 'ms-a1', base_kind: 'original', base_kind_label: '原文' },
  { i: 2, title: '第2章 灯下密信', len: 5120, manuscript_count: 1, adapted_count: 0,
    base_manuscript_id: 'ms-b1', base_kind: 'legacy', base_kind_label: '升级前保存的正文' },
];
const BOOK = {
  id: BOOK_ID, title: '长夜灯', revision: 5, chapter_count: 2,
  created_at: 1750000000000, updated_at: 1750000800000, schema_version: 2,
  source: { name: '长夜灯.txt', encoding: 'gb18030', bytes: 51234, split_level: 'heading',
    split_level_label: '按章节标题识别', max_len: 6000, total_len: 14382 },
  origin_recovery: { restored: [1], legacy: [2], reason: '导入暂存已过期或被清理，无法核对原文', matched_import: '' },
  elements: { characters: [], scenes: [], props: [], raw: '', updated_at: 0, task_id: '', version: 0 },
  elements_versions: [], outline: '', outline_versions: [], outline_version: 0,
  summaries: {}, script: [], script_versions: [], script_version: 0,
};
const TASKS = [
  { id: 'st-novel-0003', status: 'done', title: '《长夜灯》· 要素', module: 'novel', origin_label: '创作场',
    result: '## 人物\n· 沈砚：书生', result_partial: '', created_at: 1750000700000, updated_at: 1750000750000,
    novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1] },
    input: { content: 'x', module: 'novel', origin_label: '创作场', title: '《长夜灯》· 要素',
      novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1] } } },
  { id: 'st-novel-0002', status: 'failed', title: '《长夜灯》· 要素', module: 'novel', origin_label: '创作场',
    result: '', result_partial: '', error: '中转返回空流', created_at: 1750000600000, updated_at: 1750000620000,
    novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1, 2] },
    input: { content: 'x', module: 'novel', origin_label: '创作场', title: '《长夜灯》· 要素',
      novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1, 2] } } },
  { id: 'st-novel-0001', status: 'cancelled', title: '《长夜灯》· 要素', module: 'novel', origin_label: '创作场',
    result: '', result_partial: '', created_at: 1750000500000, updated_at: 1750000510000,
    novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1] },
    input: { content: 'x', module: 'novel', origin_label: '创作场', title: '《长夜灯》· 要素',
      novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1] } } },
  { id: 'st-novel-0000', status: 'interrupted', title: '《长夜灯》· 要素', module: 'novel', origin_label: '创作场',
    result: '', result_partial: '', created_at: 1750000400000, updated_at: 1750000410000,
    novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1] },
    input: { content: 'x', module: 'novel', origin_label: '创作场', title: '《长夜灯》· 要素',
      novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1] } } },
];

async function setup(page) {
  await page.route('**/api/providers/models', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ models: [{ id: 'probe-default', name: 'probe-default' }] }),
  }));
  await page.route((url) => url.pathname === '/api/studio/tasks', (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== 'GET') return route.fallback();
    const rows = url.searchParams.get('module') === 'novel' ? TASKS : [];
    return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ tasks: rows }) });
  });
  await page.route('**/api/studio/tasks/st-*', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ task: TASKS[0] }),
  }));
  await page.route('**/api/novel/**', async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json',
      body: JSON.stringify(b) });
    if (p === '/api/novel/location') {
      // 路径只用于验证「界面照抄后端给的字符串」，所以拼一段示意路径，
      // 不写本机用户目录字面量（守门 tests/test_no_absolute_paths.py）。
      const root = ['', 'data', 'studio', 'novel'].join('/');
      return json({ data_dir: root.replace('/studio/novel', ''), novel_root: root,
        can_open_folder: true, export_note: '导出由浏览器发起下载。' });
    }
    if (p === '/api/novel/books' && route.request().method() === 'GET') {
      return json({ books: [BOOK], corrupt: [] });
    }
    if (p === `/api/novel/books/${BOOK_ID}` && route.request().method() === 'GET') {
      return json({ book: BOOK, chapters: CHAPTERS, broken: [], location: 'X:/smoke' });
    }
    const chapterMatch = p.match(new RegExp(`^/api/novel/books/${BOOK_ID}/chapters/(\\d+)$`));
    if (chapterMatch) {
      const no = Number(chapterMatch[1]);
      return json({
        i: no, title: CHAPTERS[no - 1].title, len: CHAPTERS[no - 1].len, revision: 3,
        base_manuscript_id: CHAPTERS[no - 1].base_manuscript_id,
        content_manuscript_id: CHAPTERS[no - 1].base_manuscript_id,
        content: '正文内容。'.repeat(60),
        manuscripts: [
          { id: CHAPTERS[no - 1].base_manuscript_id,
            kind: CHAPTERS[no - 1].base_kind,
            kind_label: CHAPTERS[no - 1].base_kind_label,
            no: 0, label: CHAPTERS[no - 1].base_kind === 'legacy' ? '升级前保存的正文' : '原文',
            len: CHAPTERS[no - 1].len, created_at: 1750000000000,
            source_manuscript_id: '', task_id: '', user_request: '', broken: false },
        ],
      });
    }
    if (p === '/api/novel/stage/preview') {
      const body = JSON.parse(route.request().postData() || '{}');
      const picked = body.chapter_ids || [];
      const bodyChars = Number(body.body_chars || 3000);
      return json({
        stage: body.stage, limits: { body_chars: bodyChars, max_chapters: Number(body.max_chapters || 6) },
        warnings: [], chapters: [], sources: { chapters: [], elements_version: 0, outline_version: 0,
          uses_outline: false, uses_characters: false, has_summaries: false },
        budget: {
          chars: 12000, total_source_chars: 9000 * picked.length,
          used_chars: Math.min(9000, bodyChars) * picked.length,
          dropped_chars: Math.max(0, 9000 - bodyChars) * picked.length,
          estimated_tokens: 13500, estimate_note: '字数是字符数；token 为估算值',
          context_window: 128000, context_known: true, output_reserve: 4096,
          available_tokens: 123904, fits: true, suggestions: [],
          body_limit: bodyChars, max_chapters: Number(body.max_chapters || 6), chapter_count: picked.length,
        },
      });
    }
    return json({ error: `no mock: ${p}` }, 404);
  });
}

async function openElements(page) {
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);
  const onboarding = await page.evaluate(() => document.querySelector('#onboardingDialog')?.open);
  if (onboarding) {
    await page.click('#onboardingDialog [data-close="onboardingDialog"]');
    await page.waitForTimeout(300);
  }
  await page.click('#openStudio');
  await page.waitForTimeout(1000);
  await page.locator('#studioPages [data-studio-page="novel"]').click();
  await page.waitForTimeout(300);
  await page.locator('#novelBooksBtn').click();
  await page.waitForTimeout(500);
  await page.locator('#novelBookList [data-novel-open]').first().click({ force: true });
  await page.waitForTimeout(1000);
  await page.locator('#novelStages [data-novel-stage="elements"]').click();
  await page.waitForTimeout(800);
  // 勾选两章
  for (const no of [1, 2]) {
    const box = page.locator(`#novelChapterList [data-novel-pick="${no}"]`);
    if (await box.count()) await box.check();
  }
  await page.waitForTimeout(700);
}

(async () => {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  for (const vp of [
    { name: 'desktop-1440x900', width: 1440, height: 900 },
    { name: 'portrait-390x844', width: 390, height: 844 },
    { name: 'landscape-844x390', width: 844, height: 390 },
  ]) {
    for (const scheme of ['light', 'dark']) {
      const context = await browser.newContext({
        viewport: { width: 1440, height: 900 }, colorScheme: scheme,
      });
      const page = await context.newPage();
      await setup(page);
      await openElements(page);
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.waitForTimeout(400);
      if (vp.width <= 760) {
        await page.locator('#novelPanes [data-novel-pane="editor"]').click();
        await page.waitForTimeout(250);
      }
      await page.screenshot({ path: path.join(SHOT_DIR, `${vp.name}-${scheme}-elements.png`) });
      if (vp.width <= 760) {
        await page.locator('#novelPanes [data-novel-pane="preview"]').click();
        await page.waitForTimeout(250);
        await page.screenshot({ path: path.join(SHOT_DIR, `${vp.name}-${scheme}-elements-preview.png`) });
      }
      await context.close();
    }
  }
  await browser.close();
  console.log('阶段截图完成');
})();
