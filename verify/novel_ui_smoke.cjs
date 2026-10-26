// 创作场（小说改编阶段一）前端冒烟：桌面 + 手机横屏 + 手机竖屏。
//
// 断言：侧栏出现「创作场」分页且可切换、切页不丢其它页状态；导入全链路（选文件 →
// 拆章索引 → 勾选 → 保存为作品）；降级级别明示；单章查看/编辑/保存；导出；作品列表
// 打开与删除确认；手机竖屏三档切换（同一时刻只显示一个面板、隐藏面板不可聚焦）；
// 手机横屏三栏同时可见；无 pageerror / console.error。
//
// 全部走路由注入：文件用内存 Blob 造（不读真实书），接口按契约回假数据，
// 只连 127.0.0.1 的隔离实例，不碰 8765/8876、不调真实模型。
// 运行（先起隔离源码实例 verify\_serve_tmp.py）：
//   node verify\novel_ui_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8892';
const SHOT_DIR = process.env.NAIBA_SMOKE_SHOTS || '';

const IMPORT_ID = 'import-20260101-abcdefabcdef';
const BOOK_ID = 'book-20260101-abcdefabcdef';
const CHAPTERS = [
  { i: 1, title: '第1章 雨夜来客', len: 4860 },
  { i: 2, title: '第2章 灯下密信', len: 5120 },
  { i: 3, title: '第3章 不归桥', len: 4402 },
];
const IMPORT_PAYLOAD = {
  import_id: IMPORT_ID,
  name: '长夜灯.txt',
  encoding: 'gb18030',
  bytes: 51234,
  max_len: 6000,
  split_level: 'heading',
  split_level_label: '按章节标题识别',
  total: CHAPTERS.length,
  total_len: 14382,
  chapters: CHAPTERS,
};
const BOOK = {
  id: BOOK_ID,
  title: '长夜灯',
  revision: 3,
  chapter_count: CHAPTERS.length,
  created_at: 1750000000000,
  updated_at: 1750000100000,
  source: {
    name: '长夜灯.txt', encoding: 'gb18030', bytes: 51234,
    split_level: 'heading', split_level_label: '按章节标题识别',
    max_len: 6000, total_len: 14382,
  },
  elements: { characters: ['沈砚：书生'], scenes: [], props: [], raw: '## 人物\\n· 沈砚：书生', updated_at: 1750000400000, task_id: 'old-elements', version: 1 },
  elements_versions: [
    { version: 1, raw: '## 人物\\n· 旧版人物', updated_at: 1750000400000, task_id: 'old-elements', chapter_ids: [1] },
    { version: 2, raw: '## 人物\\n· 沈砚：书生', updated_at: 1750000500000, task_id: 'new-elements', chapter_ids: [1, 2] },
  ],
  outline: '', outline_versions: [],
  script_version: 2,
  script: [
    { i: 1, scene: 'INT. 新场景 - 夜', content: '新版场次。', from_chapter: 1 },
  ],
  script_versions: [
    { version: 1, script: [{ i: 1, scene: 'INT. 旧场景 - 夜', content: '旧版场次。', from_chapter: 1 }], updated_at: 1750000400000, task_id: 'old-script', chapter_ids: [1] },
    { version: 2, script: [{ i: 1, scene: 'INT. 新场景 - 夜', content: '新版场次。', from_chapter: 1 }], updated_at: 1750000500000, task_id: 'new-script', chapter_ids: [1, 2] },
  ],
};
const CHAPTER_BODY = {
  i: 1, title: '第1章 雨夜来客', len: 4860, revision: 1,
  content: '雨下到三更，青石街尽头的灯笼只剩一盏。沈砚撑伞走过长桥时，听见桥下传来三声极轻的叩击。',
};

// ---- 阶段二：要素 / 大纲 ----
const STAGE_TASK_ID = 'st-novel-0001';
const STAGE_ELEMENTS_TEXT = '## 人物\n· 沈砚：书生，谨慎\n· 老周：船夫\n## 场景\n· 长桥：第1章\n## 道具\n· 无名信：推动调查';
const STAGE_TASK = {
  id: STAGE_TASK_ID, status: 'done', detail: '', error: '',
  title: '《长夜灯》· 要素', module: 'novel', origin_label: '创作场',
  result: STAGE_ELEMENTS_TEXT, result_partial: '', usage: {},
  created_at: 1750000200000, updated_at: 1750000300000,
  novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1, 2] },
  input: {
    content: '素材', model_key: 'online:probe-te', model_name: 'probe-default',
    title: '《长夜灯》· 要素', module: 'novel', origin_label: '创作场',
    novel: { book_id: BOOK_ID, stage: 'elements', chapter_ids: [1, 2] },
  },
};
// 模块库分区的任务：创作场页绝不能看到它（分区互不串台）。
const LIBRARY_TASK = {
  id: 'st-library-0001', status: 'done', title: '模板任务', module: 'global',
  origin_label: '文生图', result: '模板生成结果', result_partial: '',
  created_at: 1750000100000, updated_at: 1750000100000,
  input: { content: 'x', module: 'global', origin_label: '文生图' },
};

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok: !!ok, detail });
  if (!ok) console.error(`FAIL ${name} ${detail}`);
}

async function main() {
  // 本机没有 playwright 自带 chromium：与仓库其它冒烟脚本一致走系统 msedge。
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  const errors = [];
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const text = message.text();
    const url = message.location()?.url || '';
    // 隔离实例的供应商目录指向 example.invalid，模型目录探测 400 是预期噪声
    // （与 studio_ui_smoke 同口径：不评模型能力，只验证 UI 结构与交互）。
    if (text.includes('status of 400 (Bad Request)') && url.includes('/api/providers/models')) return;
    errors.push(`console.error: ${text}${url ? ` @ ${url}` : ''}`);
  });

  // ---- 路由注入：/api/novel/* 全部走假实现 ----
  let imported = null;
  let savedBody = null;
  let deletedBook = false;
  let stageSubmit = null;
  let applySubmit = null;
  // 两级模型选择：记录每次 /api/providers/models 请求，并回两家不同的目录。
  const modelRequests = [];
  const MODEL_CATALOG = {
    'online:probe-te': ['probe-alpha', 'probe-beta', 'probe-gamma'],
    'online:probe-moda': ['qwen-max', 'qwen-plus'],
  };
  let scenePatch = null;
  let reorderSubmit = null;
  const exportQueries = [];
  // 剧本场次的服务端状态（删除/重排后要能反映出来）。
  let scriptState = [
    { i: 1, scene: 'INT. 新场景 - 夜', content: '新版场次。', from_chapter: 1 },
  ];
  let stageWarnings = [];
  let stageTaskState = { ...STAGE_TASK, status: 'queued', result: '', result_partial: '' };
  let stageTaskPolls = 0;
  const studioTaskQueries = [];

  // 工作台任务分区：记录请求过的 query，据此回不同分区（验证两边互不串台）。
  // 注意用谓词而不是 glob：glob 里的 `?` 是单字符通配，`tasks?*` 会把
  // /api/studio/tasks/<id> 详情也一起吞掉。
  await page.route(
    (url) => url.pathname === '/api/studio/tasks',
    (route) => {
      const url = new URL(route.request().url());
      if (route.request().method() !== 'GET') return route.fallback();
      studioTaskQueries.push(url.search);
      if (url.searchParams.get('module') === 'novel') {
        return route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ tasks: [stageTaskState] }),
        });
      }
      if (url.searchParams.get('exclude_module') === 'novel') {
        return route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ tasks: [LIBRARY_TASK] }),
        });
      }
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ tasks: [LIBRARY_TASK, stageTaskState] }),
      });
    },
  );
  // 任务详情 / 事件：轮询 3 次后转 done，用来验证「排队 → 完成 → 可写回」。
  // 同样用谓词匹配：带查询串的 events 用 glob 不好写，容易被漏掉而打到真实接口。
  await page.route(
    (url) => url.pathname.startsWith('/api/studio/tasks/st-novel-'),
    (route) => {
      const request = route.request();
      if (request.method() !== 'GET') return route.fallback();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/events')) {
        return route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ events: [], next: 0, terminal: stageTaskState.status === 'done' }),
        });
      }
      stageTaskPolls += 1;
      if (stageTaskPolls >= 3) {
        stageTaskState = { ...STAGE_TASK, status: 'done', result: STAGE_ELEMENTS_TEXT };
      } else {
        stageTaskState = { ...STAGE_TASK, status: 'running', result: '', result_partial: '正在整理人物…' };
      }
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ task: stageTaskState }),
      });
    },
  );

  // 模型目录：按 model_key 回不同目录，用来验证「换供应商就换目录」。
  await page.route(
    (url) => url.pathname === '/api/providers/models',
    (route) => {
      const body = JSON.parse(route.request().postData() || '{}');
      const key = String(body.model_key || '');
      modelRequests.push({ model_key: key });
      const models = MODEL_CATALOG[key];
      if (!models) {
        return route.fulfill({
          status: 400, contentType: 'application/json',
          body: JSON.stringify({ error: '冒烟注入：没有这个供应商的目录' }),
        });
      }
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ models: models.map((id) => ({ id, name: id })) }),
      });
    },
  );

  await page.route('**/api/novel/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const json = (body, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: JSON.stringify(body),
    });
    if (path === '/api/novel/import' && method === 'POST') {
      imported = request.postDataBuffer() ? true : true;
      return json(IMPORT_PAYLOAD);
    }
    if (path === '/api/novel/books' && method === 'POST') {
      return json({ book: BOOK }, 201);
    }
    if (path === '/api/novel/books' && method === 'GET') {
      return json({
        books: deletedBook ? [] : [{
          id: BOOK_ID, title: BOOK.title, revision: BOOK.revision,
          chapter_count: BOOK.chapter_count, created_at: BOOK.created_at,
          updated_at: BOOK.updated_at, source: BOOK.source,
        }],
        corrupt: [],
      });
    }
    if (path === `/api/novel/books/${BOOK_ID}` && method === 'DELETE') {
      deletedBook = true;
      return json({ ok: true });
    }
    if (path === `/api/novel/books/${BOOK_ID}` && method === 'GET') {
      return json({ book: BOOK, chapters: CHAPTERS, broken: [] });
    }
    if (path === `/api/novel/books/${BOOK_ID}/export`) {
      exportQueries.push(url.search);
      return json({ format: 'txt', text: '第1章 雨夜来客\n\n正文内容。\n' });
    }
    // 阶段二：生成提交（记录请求体，供断言任务契约字段）
    if (path === '/api/novel/stage' && method === 'POST') {
      stageSubmit = JSON.parse(request.postData() || '{}');
      return json({ task: { ...STAGE_TASK, status: 'queued', result: '', result_partial: '' }, warnings: stageWarnings, book_revision: BOOK.revision }, 201);
    }
    // 阶段二：写回（记录请求体）。要素与剧本各自返回对应结构。
    if (path === `/api/novel/books/${BOOK_ID}/apply` && method === 'POST') {
      applySubmit = JSON.parse(request.postData() || '{}');
      const nextRevision = (BOOK.revision || 0) + 1;
      if (applySubmit.stage === 'script') {
        const nextScript = [
          { i: 1, scene: 'INT. 新场景 - 夜', content: '新版场次。', from_chapter: 1 },
          { i: 2, scene: 'EXT. 长桥 - 清晨', content: '雾还没散。', from_chapter: 1 },
        ];
        scriptState = nextScript;
        return json({
          book: {
            ...BOOK, revision: nextRevision,
            elements: BOOK.elements, outline: BOOK.outline, summaries: {},
            script: nextScript,
            script_version: 3,
            script_versions: [...BOOK.script_versions, { version: 3, script: nextScript, updated_at: 1750000600000, task_id: 'new-script-2', chapter_ids: [1, 2] }],
            script_parsed: true,
          },
        });
      }
      return json({
        book: {
          ...BOOK, revision: nextRevision,
          elements: {
            characters: ['沈砚：书生，谨慎', '老周：船夫'],
            scenes: ['长桥：第1章'], props: ['无名信：推动调查'],
            raw: STAGE_ELEMENTS_TEXT, updated_at: 1750000600000, task_id: STAGE_TASK_ID, version: 3,
          },
          elements_versions: [...BOOK.elements_versions, { version: 3, raw: STAGE_ELEMENTS_TEXT, updated_at: 1750000600000, task_id: STAGE_TASK_ID, chapter_ids: [1, 2] }],
          outline: '', summaries: {}, script: [],
        },
      });
    }
    // 阶段三：场次 CRUD 与重排
    if (path === `/api/novel/books/${BOOK_ID}/script/reorder` && method === 'POST') {
      reorderSubmit = JSON.parse(request.postData() || '{}');
      const order = reorderSubmit.order || [];
      const byNo = {
        1: { i: 1, scene: 'INT. 改过的地点 - 夜', content: '沈砚推门进来。', from_chapter: 1 },
        2: { i: 2, scene: 'EXT. 长桥 - 清晨', content: '雾还没散。', from_chapter: 1 },
      };
      return json({
        script: order.map((no, pos) => ({ ...byNo[no], i: pos + 1 })),
        revision: 9,
      });
    }
    if (path === `/api/novel/books/${BOOK_ID}/script` && method === 'GET') {
      return json({ script: scriptState, revision: 8, parsed: true });
    }
    const sceneMatch = path.match(new RegExp(`^/api/novel/books/${BOOK_ID}/script/(\\d+)$`));
    if (sceneMatch) {
      const no = Number(sceneMatch[1]);
      if (method === 'PATCH') {
        scenePatch = JSON.parse(request.postData() || '{}');
        return json({
          scene: { i: no, scene: scenePatch.scene, content: scenePatch.content, from_chapter: 1 },
          revision: 9,
        });
      }
      if (method === 'DELETE') {
        scriptState = scriptState.filter((row) => row.i !== no).map((row, pos) => ({ ...row, i: pos + 1 }));
        return json({ ok: true, count: scriptState.length });
      }
    }
    const chapterMatch = path.match(new RegExp(`^/api/novel/books/${BOOK_ID}/chapters/(\\d+)$`));
    if (chapterMatch) {
      const no = Number(chapterMatch[1]);
      if (method === 'PATCH') {
        savedBody = JSON.parse(request.postData() || '{}');
        return json({
          chapter: {
            i: no,
            title: savedBody.title ?? `第${no}章`,
            len: String(savedBody.content || '').length,
            content: savedBody.content ?? '',
            revision: 2,
          },
        });
      }
      if (method === 'DELETE') return json({ ok: true, chapter_count: 2 });
      return json({ ...CHAPTER_BODY, i: no, title: CHAPTERS[no - 1]?.title || CHAPTER_BODY.title });
    }
    return json({ error: `未注入的接口：${method} ${path}` }, 404);
  });

  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);

  // 全新隔离实例会弹首启引导，先关掉（与 studio_ui_smoke 同做法）。
  const onboardingOpen = await page.evaluate(() => document.querySelector('#onboardingDialog')?.open);
  if (onboardingOpen) {
    await page.click('#onboardingDialog [data-close="onboardingDialog"]');
    await page.waitForTimeout(400);
  }

  // ---- 1. 进入工作台并切到创作场 ----
  // 入口与 studio_ui_smoke 一致：侧栏的 #openStudio（顶栏已无此按钮）。
  await page.click('#openStudio');
  await page.waitForTimeout(1400);
  const novelTab = page.locator('#studioPages [data-studio-page="novel"]');
  check('侧栏有创作场分页', (await novelTab.count()) === 1);
  await novelTab.click();
  await page.waitForTimeout(300);
  check('创作场页面可见', await page.locator('#studioPageNovel').isVisible());
  check('其它页隐藏', !(await page.locator('#studioPageLibrary').isVisible()));
  check('阶段导航四项', (await page.locator('#novelStages .novel-stage').count()) === 4);
  // 阶段二接入后「要素/大纲」可用；「剧本」属阶段三，仍未接入、必须显式禁用。
  // 阶段二、三都已接入：四个阶段全部可用，没有禁用项。
  check('四个阶段全部可用',
    (await page.locator('#novelStages .novel-stage:disabled').count()) === 0);

  // ---- 2. 导入全链路 ----
  await page.setInputFiles('#novelFileInput', {
    name: '长夜灯.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('第1章 雨夜来客\n雨下到三更。\n第2章 灯下密信\n信上有字。\n', 'utf8'),
  });
  await page.waitForTimeout(500);
  check('导入后出现保存为作品入口', await page.locator('#novelImportBlock').isVisible());
  const importNote = await page.locator('#novelImportNote').innerText();
  check('导入摘要明示拆分层级', importNote.includes('按章节标题识别'), importNote);
  check('导入摘要说明编码口径', importNote.includes('实际选中的解码方式'), importNote);
  check('暂存章节进入列表', (await page.locator('#novelChapterList [data-novel-pick]').count()) === 3);
  check('默认全选', (await page.locator('#novelChapterList [data-novel-pick]:checked').count()) === 3);
  check('已选计数正确', (await page.locator('#novelSelectedCount').innerText()).includes('已选 3 / 3'));

  // 取消勾选一章
  await page.locator('#novelChapterList [data-novel-pick="2"]').uncheck();
  await page.waitForTimeout(150);
  check('取消勾选后计数变化', (await page.locator('#novelSelectedCount').innerText()).includes('已选 2 / 3'));

  // ---- 3. 保存为作品 ----
  await page.locator('#novelSaveBook').click();
  await page.waitForTimeout(600);
  check('保存后进入作品态', (await page.locator('#novelBookTitle').innerText()).includes('长夜灯'));
  check('保存后导入块收起', !(await page.locator('#novelImportBlock').isVisible()));
  check('作品态章节数正确', (await page.locator('#novelStatChapters').innerText()) === '3');
  check('作品态显示已保存', (await page.locator('#novelStatState').innerText()) === '已保存');
  check('自动打开第一章', (await page.locator('#novelChapterTitle').inputValue()).includes('第1章'));

  // ---- 4. 编辑与保存 ----
  await page.locator('#novelChapterTitle').fill('第1章 改过的标题');
  await page.locator('#novelChapterBody').fill('改过的正文内容。');
  await page.waitForTimeout(150);
  check('有未保存修改时保存按钮可用', !(await page.locator('#novelSaveChapter').isDisabled()));
  await page.locator('#novelSaveChapter').click();
  await page.waitForFunction(() => document.querySelector('#novelSaveState')?.textContent === '已保存', null, { timeout: 5000 });
  check('保存请求带上 revision', savedBody && savedBody.revision === 1, JSON.stringify(savedBody));
  check('保存后列表标题同步', (await page.locator('#novelChapterList').innerText()).includes('改过的标题'));
  check('保存状态显示已保存', (await page.locator('#novelSaveState').innerText()) === '已保存');

  // ---- 5. 章节筛选 ----
  await page.locator('#novelChapterFilter').fill('不归');
  await page.waitForTimeout(200);
  check('筛选生效', (await page.locator('#novelChapterList [data-novel-chapter]').count()) === 1);
  await page.locator('#novelChapterFilter').fill('');
  await page.waitForTimeout(200);

  // ---- 6. 导出（点按钮开菜单，选一项才下载） ----
  await page.locator('#novelExportBtn').click();
  await page.waitForTimeout(200);
  check('导出菜单打开', await page.locator('#novelExportMenu').isVisible());
  const downloadPromise = page.waitForEvent('download', { timeout: 5000 }).catch(() => null);
  await page.locator('#novelExportMenu [data-novel-export="txt"]').click();
  const download = await downloadPromise;
  check('导出 TXT 触发下载',
    Boolean(download) && (await download.suggestedFilename()).endsWith('.txt'),
    download ? await download.suggestedFilename() : '无下载事件');

  // ---- 7. 作品列表与删除确认 ----
  await page.locator('#novelBooksBtn').click();
  await page.waitForTimeout(400);
  check('作品列表弹窗打开', await page.locator('#novelBooksDialog').isVisible());
  check('列表里有作品', (await page.locator('#novelBookList .novel-book-row').count()) === 1);
  await page.locator('#novelBooksClose').click();
  await page.waitForTimeout(200);

  // ---- 7b. 阶段二：要素生成全链路 ----
  await page.locator('#novelStages [data-novel-stage="elements"]').click();
  await page.waitForTimeout(600);
  check('切到要素阶段', await page.locator('#novelStagePanel').isVisible());
  check('要素阶段隐藏章节编辑', !(await page.locator('#novelEditorFields').isVisible()));
  check('要素阶段标题正确',
    (await page.locator('#novelEditorHead').innerText()) === '要素');
  check('未勾选章节时不能生成',
    (await page.locator('#novelStageGenerate').isDisabled())
    || (await page.locator('#novelStageWarning').innerText()).includes('勾选'));

  // 勾选两章当素材
  await page.locator('#novelChapterList [data-novel-pick="1"]').check();
  await page.locator('#novelChapterList [data-novel-pick="2"]').check();
  await page.waitForTimeout(300);
  check('勾选后素材章节数更新',
    (await page.locator('#novelStageMeta').innerText()).includes('2 章'),
    await page.locator('#novelStageMeta').innerText());

  // 模型选择与提词库同口径：先选供应商，再选该供应商目录里的模型
  check('供应商下拉有可选项',
    (await page.locator('#novelStageProvider option').count()) > 0);
  const providerValue = await page.locator('#novelStageProvider').inputValue();
  check('供应商已选中一项', Boolean(providerValue), providerValue);
  const modelOptions = await page.locator('#novelStageModel option').allInnerTexts();
  check('模型下拉拉到了该供应商的完整目录',
    modelOptions.length === 3 && modelOptions.includes('probe-default') === false,
    JSON.stringify(modelOptions));
  check('模型请求带 model_key',
    modelRequests.some((row) => row.model_key === providerValue),
    JSON.stringify(modelRequests));
  check('模型已选中一项',
    Boolean(await page.locator('#novelStageModel').inputValue()));

  // 切换供应商 → 重新拉那一家的目录（缓存过的不再请求）
  await page.locator('#novelStageProvider').selectOption('online:probe-moda');
  await page.waitForTimeout(700);
  const secondModels = await page.locator('#novelStageModel option').allInnerTexts();
  check('换供应商后模型目录跟着换',
    secondModels.includes('qwen-max') && !secondModels.includes('probe-alpha'),
    JSON.stringify(secondModels));
  check('换供应商会重新请求目录',
    modelRequests.filter((row) => row.model_key === 'online:probe-moda').length === 1,
    JSON.stringify(modelRequests));
  // 切回原供应商：命中缓存，创作场不再发请求。
  // 注意：`/api/providers/models` 也被聊天输入区（populateComposerModels）与提词库
  // （syncStudioModel）调用，所以这里只统计「创作场自己发的」次数——按调用时序对不上，
  // 改用请求去重后比较：切回前记下次数，切回后必须不变。
  const beforeSwitchBack = modelRequests.length;
  await page.locator('#novelStageProvider').selectOption(providerValue);
  await page.waitForTimeout(600);
  check('切回已缓存供应商不重复请求',
    modelRequests.length === beforeSwitchBack,
    `${beforeSwitchBack} → ${modelRequests.length}`);

  // 生成：断言任务契约字段
  await page.locator('#novelStageGenerate').click();
  await page.waitForTimeout(900);
  check('提交阶段请求', Boolean(stageSubmit), JSON.stringify(stageSubmit));
  if (stageSubmit) {
    check('提交带 book_id/stage/chapter_ids',
      stageSubmit.book_id === BOOK_ID && stageSubmit.stage === 'elements'
      && Array.isArray(stageSubmit.chapter_ids) && stageSubmit.chapter_ids.length === 2,
      JSON.stringify(stageSubmit));
    check('提交带模型', Boolean(stageSubmit.model_name), JSON.stringify(stageSubmit));
  }
  check('生成后出现任务区', await page.locator('#novelStageTask').isVisible());

  // 轮询到完成
  await page.waitForTimeout(4200);
  const taskState = await page.locator('#novelStageTaskState').innerText();
  check('任务状态推进到已完成', taskState === '已完成', taskState);
  check('结果正文出现',
    (await page.locator('#novelStageResult').innerText()).includes('沈砚'),
    await page.locator('#novelStageResult').innerText());
  check('完成后出现写回按钮', await page.locator('#novelStageApply').isVisible());

  // 写回
  page.once('dialog', (dialog) => dialog.accept());
  await page.locator('#novelStageApply').click();
  await page.waitForTimeout(700);
  check('写回请求带 stage/task_id/revision',
    applySubmit && applySubmit.stage === 'elements'
    && applySubmit.task_id === STAGE_TASK_ID
    && typeof applySubmit.revision === 'number',
    JSON.stringify(applySubmit));
  check('写回后显示已保存结果',
    await page.locator('#novelStageSaved').isVisible());
  check('已保存结果含人物',
    (await page.locator('#novelStageSavedBody').innerText()).includes('沈砚'),
    await page.locator('#novelStageSavedBody').innerText());
  const elementVersionLabels = await page.locator('#novelStageVersion option').allInnerTexts();
  check('要素版本选项包含时间和来源章节',
    elementVersionLabels.some((label) => label.includes('2025-06-15') && label.includes('第 1 章')),
    JSON.stringify(elementVersionLabels));
  await page.locator('#novelStageVersion').selectOption('1');
  check('切换要素版本后显示对应正文',
    (await page.locator('#novelStageSavedBody').innerText()).includes('旧版人物'),
    await page.locator('#novelStageSavedBody').innerText());
  check('保存后无活动任务时任务历史仍显示', await page.locator('#novelTaskHistory').isVisible());

  // 分区：创作场只请求 module=novel
  check('创作场任务请求带 module=novel',
    studioTaskQueries.some((q) => q.includes('module=novel')), JSON.stringify(studioTaskQueries));
  check('创作场任务列表不含模块库任务',
    !(await page.locator('#novelStageResult').innerText()).includes('模板生成结果'));

  // ---- 7c. 大纲阶段共用同一套面板 ----
  await page.locator('#novelStages [data-novel-stage="outline"]').click();
  await page.waitForTimeout(600);
  check('切到大纲阶段', (await page.locator('#novelEditorHead').innerText()) === '改编大纲');
  check('大纲阶段仍显示生成入口', await page.locator('#novelStageGenerate').isVisible());
  check('大纲使用独立生成面板', await page.locator('[data-stage-panel="outline"]#novelStagePanel').count() === 1);
  check('要素结果框与大纲结果框分离', await page.locator('[data-stage-panel="outline"] #novelStageResult').count() === 1);
  check('大纲不依赖要素版本', await page.locator('[data-stage-panel="outline"] [data-novel-context="outline"]').count() === 0);

  // ---- 7d. 阶段三：剧本场次 ----
  await page.locator('#novelStages [data-novel-stage="script"]').click();
  await page.waitForTimeout(500);
  check('切到剧本阶段', (await page.locator('#novelEditorHead').innerText()) === '剧本');
  check('剧本阶段显示场次区', await page.locator('#novelSceneBlock').isVisible());
  check('剧本可分别选择人物和大纲版本',
    await page.locator('[data-stage-panel="script"] [data-novel-context="script"] [data-context-elements]').count() === 1
    && await page.locator('[data-stage-panel="script"] [data-novel-context="script"] [data-context-outline]').count() === 1);

  // 生成剧本（复用阶段二的提交契约，stage=script）
  await page.locator('#novelStageGenerate').click();
  await page.waitForTimeout(900);
  check('剧本提交带 stage=script', stageSubmit && stageSubmit.stage === 'script',
    JSON.stringify(stageSubmit));
  await page.waitForTimeout(4200);

  // 写回：解析成两场
  page.once('dialog', (dialog) => dialog.accept());
  await page.locator('#novelStageApply').click();
  await page.waitForTimeout(800);
  check('剧本写回成功', (await page.locator('#novelSceneList .novel-scene-row').count()) === 2,
    await page.locator('#novelSceneCount').innerText());
  check('场次显示来源章节',
    (await page.locator('#novelSceneList').innerText()).includes('第 1 章'),
    await page.locator('#novelSceneList').innerText());

  // 编辑单场
  await page.locator('#novelSceneList [data-novel-scene-open="1"]').click();
  await page.waitForTimeout(300);
  check('点场次展开编辑区', await page.locator('#novelSceneEdit').isVisible());
  await page.locator('#novelScriptVersion').selectOption('1');
  check('剧本版本下拉包含时间和来源章节',
    (await page.locator('#novelScriptVersion option').first().innerText()).includes('第 1 章'));
  check('切到历史剧本后显示旧版且只读',
    (await page.locator('#novelSceneList').innerText()).includes('旧场景')
      && await page.locator('#novelSceneEdit').isHidden()
      && await page.locator('#novelSceneList [data-novel-scene-open]').isDisabled());
  await page.locator('#novelScriptVersion').selectOption('3');
  check('切回最新剧本后可编辑当前版本',
    (await page.locator('#novelSceneList').innerText()).includes('新场景'));
  await page.locator('#novelSceneList [data-novel-scene-open="1"]').click();
  check('切回最新后重新打开场次编辑区', await page.locator('#novelSceneEdit').isVisible());
  await page.locator('#novelSceneHead').fill('INT. 改过的地点 - 夜');
  await page.locator('#novelSceneSave').click();
  await page.waitForTimeout(600);
  check('保存场次请求带 revision', scenePatch && typeof scenePatch.revision === 'number',
    JSON.stringify(scenePatch));
  check('场次标题已更新',
    (await page.locator('#novelSceneList').innerText()).includes('改过的地点'),
    await page.locator('#novelSceneList').innerText());

  // 上下移
  const beforeOrder = await page.locator('#novelSceneList .novel-scene-open strong').allInnerTexts();
  await page.locator('#novelSceneList [data-novel-scene-action="down"][data-novel-scene-no="1"]').click();
  await page.waitForTimeout(600);
  const afterOrder = await page.locator('#novelSceneList .novel-scene-open strong').allInnerTexts();
  check('下移改变场次顺序',
    beforeOrder[0] === afterOrder[1] && beforeOrder[1] === afterOrder[0],
    `${JSON.stringify(beforeOrder)} → ${JSON.stringify(afterOrder)}`);
  check('重排请求带完整顺序', Array.isArray(reorderSubmit?.order) && reorderSubmit.order.length === 2,
    JSON.stringify(reorderSubmit));

  // 删除一场
  page.once('dialog', (dialog) => dialog.accept());
  await page.locator('#novelSceneList [data-novel-scene-action="delete"]').first().click();
  await page.waitForTimeout(700);
  check('删除后只剩一场', (await page.locator('#novelSceneList .novel-scene-row').count()) === 1);

  // 导出菜单
  await page.locator('#novelExportBtn').click();
  await page.waitForTimeout(200);
  check('导出菜单打开', await page.locator('#novelExportMenu').isVisible());
  check('导出菜单有三项', (await page.locator('#novelExportMenu [data-novel-export]').count()) === 3);
  const mdDownload = page.waitForEvent('download', { timeout: 5000 }).catch(() => null);
  await page.locator('#novelExportMenu [data-novel-export="md-extras"]').click();
  const mdFile = await mdDownload;
  check('导出 Markdown（含附录）触发下载',
    Boolean(mdFile) && (await mdFile.suggestedFilename()).endsWith('.md'),
    mdFile ? await mdFile.suggestedFilename() : '无下载事件');
  check('导出请求带 with_extras', exportQueries.some((q) => q.includes('with_extras=1')),
    JSON.stringify(exportQueries));

  // 回到拆章阶段
  await page.locator('#novelStages [data-novel-stage="split"]').click();
  await page.waitForTimeout(400);
  check('切回拆章阶段', !(await page.locator('#novelStagePanel').isVisible()));

  // ---- 8. 桌面截图 ----
  if (SHOT_DIR) {
    await page.screenshot({ path: `${SHOT_DIR}/novel_desktop_light.png`, fullPage: false });
  }

  // ---- 9. 手机竖屏：三档切换 ----
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(400);
  check('竖屏出现分档控件', await page.locator('#novelPanes').isVisible());
  const paneState = async () => page.evaluate(() => {
    const columns = document.querySelector('#novelColumns');
    return {
      pane: columns?.dataset.novelPane,
      chapters: getComputedStyle(document.querySelector('.novel-chapters')).display,
      editor: getComputedStyle(document.querySelector('.novel-editor')).display,
      preview: getComputedStyle(document.querySelector('.novel-preview')).display,
      chaptersInert: document.querySelector('.novel-chapters').inert,
      editorInert: document.querySelector('.novel-editor').inert,
    };
  });
  let state = await paneState();
  check('竖屏默认编辑档', state.pane === 'editor', JSON.stringify(state));
  check('竖屏只显示一个面板',
    state.editor !== 'none' && state.chapters === 'none' && state.preview === 'none', JSON.stringify(state));
  check('隐藏面板不可聚焦', state.chaptersInert === true, JSON.stringify(state));

  await page.locator('#novelPanes [data-novel-pane="chapters"]').click();
  await page.waitForTimeout(250);
  state = await paneState();
  check('切到章节档', state.chapters !== 'none' && state.editor === 'none', JSON.stringify(state));
  check('章节档切换后作品仍在', (await page.locator('#novelBookTitle').innerText()).includes('长夜灯'));

  await page.locator('#novelPanes [data-novel-pane="preview"]').click();
  await page.waitForTimeout(250);
  state = await paneState();
  check('切到预览档', state.preview !== 'none' && state.chapters === 'none', JSON.stringify(state));
  if (SHOT_DIR) {
    await page.screenshot({ path: `${SHOT_DIR}/novel_portrait_preview.png`, fullPage: false });
  }

  // 竖屏无横向滚动
  const portraitOverflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check('竖屏无横向溢出', portraitOverflow <= 1, `溢出 ${portraitOverflow}px`);

  // ---- 10. 手机横屏：三栏同时可见 ----
  await page.setViewportSize({ width: 844, height: 390 });
  await page.waitForTimeout(400);
  const landscape = await page.evaluate(() => {
    const pick = (sel) => getComputedStyle(document.querySelector(sel)).display;
    const columns = document.querySelector('#novelColumns');
    return {
      columns: columns ? getComputedStyle(columns).gridTemplateColumns : '',
      chapters: pick('.novel-chapters'),
      editor: pick('.novel-editor'),
      preview: pick('.novel-preview'),
      panesVisible: getComputedStyle(document.querySelector('#novelPanes')).display !== 'none',
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  });
  check('横屏三栏同时可见',
    landscape.chapters !== 'none' && landscape.editor !== 'none' && landscape.preview !== 'none',
    JSON.stringify(landscape));
  check('横屏隐藏分档控件', landscape.panesVisible === false, JSON.stringify(landscape));
  check('横屏三栏宽度符合约定',
    /170px/.test(landscape.columns) && /198px/.test(landscape.columns), landscape.columns);
  check('横屏无横向溢出', landscape.overflow <= 1, `溢出 ${landscape.overflow}px`);
  if (SHOT_DIR) {
    await page.screenshot({ path: `${SHOT_DIR}/novel_landscape_844.png`, fullPage: false });
  }

  // ---- 11. 切页不丢状态 ----
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(300);
  await page.locator('#studioPages [data-studio-page="library"]').click();
  await page.waitForTimeout(300);
  await page.locator('#studioPages [data-studio-page="novel"]').click();
  await page.waitForTimeout(400);
  check('切页返回后作品仍在', (await page.locator('#novelBookTitle').innerText()).includes('长夜灯'));

  // 收尾：把「上次所在页」还原成提词库。功能页偏好落在服务端配置里，
  // 留在 novel 会让后续 studio_ui_smoke 从创作场起步而大面积误报。
  await page.locator('#studioPages [data-studio-page="library"]').click();
  await page.waitForTimeout(500);

  // ---- 12. 无 pageerror / console.error ----
  const realErrors = errors.filter((line) => !/favicon|ERR_INTERNET|net::ERR/.test(line));
  check('无 pageerror / console.error', realErrors.length === 0, realErrors.join(' | '));

  await browser.close();

  const failed = results.filter((row) => !row.ok);
  console.log(`\n${results.length - failed.length}/${results.length} 通过`);
  for (const row of failed) console.log(`  FAIL ${row.name} ${row.detail}`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((error) => {
  console.error('冒烟脚本异常：', error);
  process.exit(2);
});
