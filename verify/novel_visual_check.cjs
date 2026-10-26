// 创作场视觉验收：桌面 1440/1280、手机横屏 844/812、竖屏 390/375，浅色与深色。
// 只截图与量尺寸，不做业务断言（业务断言在 novel_ui_smoke.cjs）。
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8892';
const SHOT_DIR = process.env.NAIBA_SHOT_DIR || path.join(__dirname, '_tmp_novel_shots');

const BOOK_ID = 'book-20260101-abcdefabcdef';
const CHAPTERS = [
  { i: 1, title: '第1章 雨夜来客', len: 4860, manuscript_count: 3, adapted_count: 2,
    base_manuscript_id: 'ms-a1', base_kind: 'original', base_kind_label: '原文' },
  { i: 2, title: '第2章 灯下密信', len: 5120, manuscript_count: 2, adapted_count: 1,
    base_manuscript_id: 'ms-b1', base_kind: 'legacy', base_kind_label: '升级前保存的正文' },
  { i: 3, title: '第3章 不归桥', len: 4402, manuscript_count: 1, adapted_count: 0,
    base_manuscript_id: 'ms-c1', base_kind: 'original', base_kind_label: '原文' },
];
const LONG_BODY = '雨下到三更，青石街尽头的灯笼只剩一盏。沈砚撑伞走过长桥时，听见桥下传来三声极轻的叩击。'.repeat(6);
const MANUSCRIPTS = {
  1: [
    { id: 'ms-a1', kind: 'original', kind_label: '原文', no: 0, label: '原文', len: 4860,
      created_at: 1750000000000, source_manuscript_id: '', task_id: '', user_request: '', broken: false },
    { id: 'ms-a2', kind: 'manual', kind_label: '人工改编稿', no: 1, label: '改编稿 1', len: 4700,
      created_at: 1750000500000, source_manuscript_id: 'ms-a1', task_id: '', user_request: '', broken: false },
    { id: 'ms-a3', kind: 'ai', kind_label: 'AI 改编稿', no: 2, label: '改编稿 2', len: 5210,
      created_at: 1750000600000, source_manuscript_id: 'ms-a2', task_id: 'st-adapt-1', user_request: '改得更紧凑', broken: false },
  ],
};
const BOOK = {
  id: BOOK_ID, title: '长夜灯', revision: 5, chapter_count: 3,
  created_at: 1750000000000, updated_at: 1750000800000,
  schema_version: 2,
  source: { name: '长夜灯.txt', encoding: 'gb18030', bytes: 51234, split_level: 'heading',
    split_level_label: '按章节标题识别', max_len: 6000, total_len: 14382 },
  origin_recovery: { restored: [1, 3], legacy: [2], reason: '导入暂存已过期或被清理，无法核对原文',
    matched_import: '' },
  elements: { characters: ['沈砚：书生'], scenes: [], props: [], raw: '## 人物\n· 沈砚：书生',
    updated_at: 1750000400000, task_id: 'st-el', version: 1 },
  elements_versions: [{ id: 'el-1', version: 1, raw: '## 人物\n· 沈砚：书生', updated_at: 1750000400000,
    task_id: 'st-el', chapter_ids: [1] }],
  outline: '## 第一幕 建置\n- 沈砚夜行', outline_versions: [{ id: 'ol-1', version: 1,
    text: '## 第一幕 建置\n- 沈砚夜行', updated_at: 1750000400000, task_id: 'st-ol', chapter_ids: [1] }],
  outline_version: 1, outline_task_id: 'st-ol', outline_updated_at: 1750000400000,
  summaries: {}, script: [], script_versions: [], script_version: 0,
};
const TASK = {
  id: 'st-novel-0001', status: 'done', title: '《长夜灯》· 要素', module: 'novel',
  origin_label: '创作场', result: '## 人物\n· 沈砚：书生', result_partial: '',
  created_at: 1750000200000, updated_at: 1750000300000,
  novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1, 2] },
  input: { content: 'x', module: 'novel', origin_label: '创作场', title: '《长夜灯》· 要素',
    model_key: 'online:probe-te', model_name: 'probe-default',
    novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1, 2] } },
};

function chapterPayload(no) {
  const rows = MANUSCRIPTS[no] || [{ id: `ms-x${no}`, kind: 'original', kind_label: '原文', no: 0,
    label: '原文', len: CHAPTERS[no - 1].len, created_at: 1750000000000,
    source_manuscript_id: '', task_id: '', user_request: '', broken: false }];
  return {
    i: no, title: CHAPTERS[no - 1].title, len: CHAPTERS[no - 1].len, revision: 3,
    base_manuscript_id: rows[0].id, content_manuscript_id: rows[0].id,
    manuscripts: rows.map((row) => ({ ...row })), content: LONG_BODY,
  };
}

const VIEWPORTS = [
  { name: 'desktop-1440x900', width: 1440, height: 900 },
  { name: 'desktop-1280x900', width: 1280, height: 900 },
  { name: 'landscape-844x390', width: 844, height: 390 },
  { name: 'landscape-812x375', width: 812, height: 375 },
  { name: 'portrait-390x844', width: 390, height: 844 },
  { name: 'portrait-375x812', width: 375, height: 812 },
];

async function setup(page) {
  await page.route('**/api/providers/models', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ models: [{ id: 'probe-default', name: 'probe-default' }] }),
  }));
  await page.route((url) => url.pathname === '/api/studio/tasks', (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== 'GET') return route.fallback();
    if (url.searchParams.get('module') === 'novel') {
      return route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ tasks: [TASK] }) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ tasks: [] }) });
  });
  await page.route('**/api/studio/tasks/st-*', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ task: TASK }),
  }));
  await page.route('**/api/novel/**', async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json',
      body: JSON.stringify(b) });
    if (p === '/api/novel/location') {
      // 路径只用于验证「界面照抄后端给的字符串」，所以拼一段示意路径，
      // 不写本机用户目录字面量（守门 tests/test_no_absolute_paths.py）。
      const base = ['', 'data', 'studio', 'novel'].join('/');
      return json({ data_dir: base.replace('/studio/novel', ''),
        novel_root: base,
        can_open_folder: true, export_note: '导出由浏览器发起下载，通常保存到浏览器默认下载目录。' });
    }
    if (p === '/api/novel/books' && route.request().method() === 'GET') {
      return json({ books: [BOOK], corrupt: [] });
    }
    if (p === `/api/novel/books/${BOOK_ID}` && route.request().method() === 'GET') {
      return json({ book: BOOK, chapters: CHAPTERS, broken: [], location: 'X:/smoke' });
    }
    if (p === `/api/novel/books/${BOOK_ID}/records`) {
      const manuscripts = Object.entries(MANUSCRIPTS).flatMap(([no, rows]) => rows.map((row) => ({
        ...row, chapter_no: Number(no), chapter_title: CHAPTERS[Number(no) - 1].title,
      })));
      return json({
        book: BOOK, chapters: CHAPTERS.map((r) => ({ i: r.i, title: r.title })), broken: [],
        manuscripts,
        elements: BOOK.elements_versions.map((r) => ({ ...r, len: 40, manuscripts: [], kind: 'elements' })),
        outlines: BOOK.outline_versions.map((r) => ({ ...r, len: 20, manuscripts: [], kind: 'outline' })),
        scripts: [], location: 'X:/smoke',
      });
    }
    const chapterMatch = p.match(new RegExp(`^/api/novel/books/${BOOK_ID}/chapters/(\\d+)$`));
    if (chapterMatch) return json(chapterPayload(Number(chapterMatch[1])));
    if (p === '/api/novel/stage/preview') {
      const body = JSON.parse(route.request().postData() || '{}');
      const picked = body.chapter_ids || [];
      const bodyChars = Number(body.body_chars || 3000);
      return json({
        stage: body.stage, limits: { body_chars: bodyChars, max_chapters: Number(body.max_chapters || 6) },
        warnings: [], chapters: [], sources: { chapters: [], elements_version: 1,
          outline_version: 1, uses_outline: true, uses_characters: true, has_summaries: false },
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

async function openNovel(page) {
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);
  const onboarding = await page.evaluate(() => document.querySelector('#onboardingDialog')?.open);
  if (onboarding) {
    await page.click('#onboardingDialog [data-close="onboardingDialog"]');
    await page.waitForTimeout(300);
  }
  // 手机视口下侧栏入口可能收进抽屉：先按桌面宽度进工作台，再切到目标视口截图，
  // 这样测的是「同一次会话在目标尺寸下的排版」，与真实用户转屏一致。
  const target = { width: page.viewportSize().width, height: page.viewportSize().height };
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.click('#openStudio');
  await page.waitForTimeout(1000);
  await page.locator('#studioPages [data-studio-page="novel"]').click();
  await page.waitForTimeout(300);
  await page.locator('#novelBooksBtn').click();
  await page.waitForTimeout(500);
  await page.locator('#novelBookList [data-novel-open]').first().click({ force: true });
  await page.waitForTimeout(1000);
  await page.setViewportSize(target);
  await page.waitForTimeout(400);
}

(async () => {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const report = [];
  for (const vp of VIEWPORTS) {
    for (const scheme of ['light', 'dark']) {
      const context = await browser.newContext({
        viewport: { width: vp.width, height: vp.height },
        colorScheme: scheme,
      });
      const page = await context.newPage();
      await setup(page);
      await openNovel(page);
      // 三个分档都截：章节 / 编辑 / 预览，手机上才能看到每档的实际排版。
      for (const pane of ['chapters', 'editor', 'preview']) {
        const paneBtn = page.locator(`#novelPanes [data-novel-pane="${pane}"]`);
        if (await paneBtn.isVisible()) {
          await paneBtn.click();
          await page.waitForTimeout(200);
        }
        const overflow = await page.evaluate(() =>
          document.documentElement.scrollWidth - document.documentElement.clientWidth);
        report.push({ viewport: vp.name, scheme, pane, overflow });
        await page.screenshot({
          path: path.join(SHOT_DIR, `${vp.name}-${scheme}-${pane}.png`),
          fullPage: false,
        });
      }
      // 管理记录面板
      await page.locator('#novelManageBtn').click();
      await page.waitForTimeout(500);
      const manageOverflow = await page.evaluate(() =>
        document.documentElement.scrollWidth - document.documentElement.clientWidth);
      report.push({ viewport: vp.name, scheme, pane: 'manage', overflow: manageOverflow });
      await page.screenshot({ path: path.join(SHOT_DIR, `${vp.name}-${scheme}-manage.png`) });
      await page.locator('#novelManageClose').click().catch(() => {});
      await context.close();
    }
  }
  await browser.close();
  fs.writeFileSync(path.join(SHOT_DIR, '_report.json'), JSON.stringify(report, null, 1));
  const bad = report.filter((row) => row.overflow > 1);
  console.log(`截图 ${fs.readdirSync(SHOT_DIR).filter((n) => n.endsWith('.png')).length} 张`);
  console.log(bad.length ? `横向溢出：${JSON.stringify(bad)}` : '所有视口无横向溢出');
})();
