// 织书台（原创写作，module="writer"）前端冒烟：桌面 + 375px 手机视口。
// 断言：侧栏第五页「织书台」可进入（拆书匠按钮显示新名）；打开作品后左栏摘要/目录、
// 中栏三阶段（设定/大纲/正文）、右栏说明都渲染真实注入的数据；模板弹窗读真实
// templates.json 并可套用；生成提交/取消/重试走 /api/writer/* 注入路由；
// 导入启元存档两段式（预览 → 确认入库）；导出走 blob 下载；手机三档只切可见栏；
// 无 pageerror / console.error。
// 全部 API 走路由注入（隔离实例没有真实模型）：不真连供应商、不真写作品数据。
// 运行（先起隔离源码实例 verify\_serve_tmp.py）：node verify\writer_ui_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';

const BOOK_ID = 'wb-20261030-000000000001';
const IMPORT_ID = 'wi-20261030-000000000001';
const TASK_ID = 'st-writer-smoke-1';

const FAKE_BOOK_MANIFEST = {
  id: BOOK_ID, title: '雾港旧事', revision: 3, schema_version: 1,
  chapter_count: 2,
  current_setup_version_id: 'su-000000000001', current_outline_version_id: 'ol-000000000001',
  setup_versions: [
    { id: 'su-000000000001', version: 1, kind: 'generated', updated_at: 1750000000000, task_id: TASK_ID, len: 120, title: '雾港旧事' },
    { id: 'su-000000000002', version: 2, kind: 'manual', updated_at: 1750000500000, task_id: '', len: 130, title: '雾港旧事（改）' },
  ],
  outline_versions: [
    { id: 'ol-000000000001', version: 1, kind: 'generated', updated_at: 1750000100000, task_id: TASK_ID, len: 60, chapter_count: 2, source_setup_version_id: 'su-000000000001' },
  ],
  active_pipeline: null,
  created_at: 1750000000000, updated_at: 1750000500000,
};

const FAKE_SETUP = {
  id: 'su-000000000001', version: 1, kind: 'generated', updated_at: 1750000000000, task_id: '',
  idea: '修船工在父亲留下的旧船里翻出一张二十年前的合影，照片上多了一个人。',
  genre: { channel: 'male', cat: '都市悬疑', sub_cat: '', tone: '克制冷峻', hook: '' },
  genre_tags: ['悬疑', '旧案'],
  title: '雾港旧事',
  title_candidates: ['雾港旧事', '铁锈退潮时'],
  blurb: '一张照片上多出的人影，把沉船旧案重新拖回水面。',
  blurb_candidates: [],
  worldview: '九十年代改制后衰落的滨海工业城；码头分区即权力格局。',
  characters: [
    { name: '陈砚', role: '主角', identity: '修船工', goal: '查清父亲沉船案', traits: '话少、认死理', speech: '短句', arc: '从旁观到入局', background: '东区老船厂' },
  ],
  style: 'sus',
  constraints: { vibe: '《漫长的季节》的潮湿感', avoid: '不要灵异' },
};

const FAKE_OUTLINE = {
  id: 'ol-000000000001', version: 1, kind: 'generated', updated_at: 1750000100000, task_id: '',
  source_setup_version_id: 'su-000000000001',
  chapters: [
    { no: 1, title: '归港的旧船', beat: '旧船入坞，照片出现第六个人。', hook: '照片背面多了一个人影' },
    { no: 2, title: '锈迹里的名字', beat: '陈砚私下打听，被明确警告。', hook: '' },
  ],
};

const FAKE_CHAPTERS = [
  { no: 1, title: '归港的旧船', beat: '旧船入坞，照片出现第六个人。', hook: '照片背面多了一个人影', revision: 2, current_version_id: 'cv-000000000001', current_kind: 'generated', len: 3120, version_count: 1 },
  { no: 2, title: '锈迹里的名字', beat: '陈砚私下打听，被明确警告。', hook: '', revision: 1, current_version_id: '', current_kind: '', len: 0, version_count: 0 },
];

const FAKE_BOOK_DETAIL = () => ({
  book: { ...FAKE_BOOK_MANIFEST, active_pipeline: currentPipeline() },
  setup: FAKE_SETUP,
  setup_current: { id: 'su-000000000001' },
  outline: FAKE_OUTLINE,
  outline_current: { id: 'ol-000000000001' },
  chapters: FAKE_CHAPTERS.map((row) => ({ ...row })),
  broken: [],
  location: 'D:\\fake-data\\studio\\writer\\wb-20261030-000000000001',
});

const FAKE_CHAPTER_1 = {
  no: 1, title: '归港的旧船', beat: '旧船入坞，照片出现第六个人。', hook: '照片背面多了一个人影',
  revision: 2, current_version_id: 'cv-000000000001',
  content: '雨是从四点四十分开始下的。陈砚把扳手丢进工具箱，抬头看了一眼天。',
  versions: [
    { id: 'cv-000000000001', version: 1, kind: 'generated', updated_at: 1750000200000, task_id: TASK_ID, len: 3120, finish_reason: '' },
  ],
};

function runningPipeline() {
  return {
    id: 'pl-000000000001', kind: 'setup_all', attempt: 1, status: 'running',
    steps: ['genre', 'title', 'blurb', 'world', 'chars', 'outline'], step_index: 1,
    current_task_id: TASK_ID, completed_task_ids: [TASK_ID],
    step_outputs: {},
    request_snapshot: { kind: 'setup_all', chapter_no: 0, model_name: 'probe-default', char_count: 4, chapter_count: 12, book_title: '雾港旧事' },
    source_version_ids: { setup: 'su-000000000001', outline: '' },
    base_revision: 3, chapter_revision: null, error: '',
    created_at: 1750000600000, updated_at: 1750000600000,
  };
}
function failedPipeline() {
  const pipeline = runningPipeline();
  pipeline.status = 'failed';
  pipeline.error = '模型输出不是合法 JSON（只接受纯 JSON 或单个代码围栏）；可重试或换模型';
  pipeline.current_task_id = TASK_ID;
  return pipeline;
}
let pipelineState = 'none'; // none | running | failed
let currentBookId = BOOK_ID; // 最近一次被读取的作品 id（注入任务归属跟随它）
function currentPipeline() {
  if (pipelineState === 'running') return runningPipeline();
  if (pipelineState === 'failed') return failedPipeline();
  return null;
}

const FAKE_TASK = {
  id: TASK_ID, status: 'running', detail: '', error: '',
  title: '《雾港旧事》· 题材推断', module: 'writer', origin_label: '织书台',
  result: '', result_partial: '【题材推断】这是正在流式产出的前半段', reasoning: [],
  usage: {}, performance: {}, finish_reason: '', handoff: null,
  created_at: 1750000600000, updated_at: 1750000600000, started_at: 1750000600000,
  // 列表 slim 的形状：writer 字段已从 input 提升到顶层（naiba/studio/service.py api_tasks）。
  writer: { book_id: BOOK_ID, pipeline_id: 'pl-000000000001', step: 'genre', chapter_no: 0 },
  input: {
    content: '生成输入', model_key: 'online:probe-writer', model_name: 'probe-default',
    title: '《雾港旧事》· 题材推断', module: 'writer', origin_label: '织书台',
    writer: { book_id: BOOK_ID, pipeline_id: 'pl-000000000001', attempt: 1, step: 'genre', chapter_no: 0 },
  },
};

const FAKE_WRITER_TASK_DONE = {
  id: 'st-writer-smoke-done', status: 'done', title: '《雾港旧事》· 正文', module: 'writer',
  origin_label: '织书台', created_at: 1750000300000, updated_at: 1750000400000,
  result: '第一章的正文结果。', result_partial: '', error: '',
  writer: { book_id: BOOK_ID, pipeline_id: 'pl-0', step: 'draft', chapter_no: 1 },
  input: { content: 'x', model_key: 'online:probe-writer', model_name: 'probe-default', module: 'writer', origin_label: '织书台', writer: { book_id: BOOK_ID, pipeline_id: 'pl-0', attempt: 1, step: 'draft', chapter_no: 1 } },
};

const FAKE_LOCATION = {
  data_dir: 'D:\\fake-data', writer_root: 'D:\\fake-data\\studio\\writer',
  can_open_folder: true, export_note: '导出由浏览器发起下载。',
};

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile', width: 375, height: 812 },
];

async function runViewport(browser, vp) {
  // 每轮视口都从干净状态开始：pipelineState/currentBookId 是模块级变量，
  // 桌面轮结束时它们是 running/成品 id，会污染手机轮注入的「作品详情」。
  pipelineState = 'none';
  currentBookId = BOOK_ID;
  const errors = [];
  const checks = [];
  const recorded = { pipelines: [], setups: [], cancels: 0, exports: 0, creates: 0, commits: 0, settingsPosts: 0 };
  const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    // 预期中的 401（隔离实例带口令但这里不带）不视为错误。
    if (text.includes('401')) return;
    errors.push(`console.error: ${text}`);
  });

  const json = (body, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) });

  // ---- 通用：bootstrap 注入测试模型；runs 空 ----
  await page.route('**/api/bootstrap', async (route) => {
    const response = await route.fetch();
    let body;
    try { body = await response.json(); } catch { return route.fulfill({ response }); }
    const fake = { model_key: 'online:probe-writer', name: '织书台冒烟模型', kind: 'online', model: 'probe-default' };
    body.model_profiles = [fake];
    body.providers = [fake];
    body.default_model_key = 'online:probe-writer';
    return route.fulfill({ response, body: JSON.stringify(body) });
  });
  await page.route('**/api/runs*', (route) => route.fulfill(json({ runs: [] })));
  await page.route('**/api/settings', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    recorded.settingsPosts += 1;
    return route.fulfill(json({ ok: true }));
  });
  await page.route('**/api/providers/models', (route) => route.fulfill(json({ models: ['probe-default', 'probe-second'] })));

  // ---- writer API 注入 ----
  // 兜底（最先注册 = 优先级最低）：任意 wb-* 作品详情/删改；后面再注册更具体的
  // pipeline / chapters / export 路由盖过它（playwright 后注册者优先）。
  // currentBookId 跟随最近一次被读取的作品：注入的任务归属要跟它一致，
  // 管理记录的「生成记录」分区才筛得出（与真实 api_tasks 的 writer.book_id 提升同形状）。
  await page.route('**/api/writer/books/wb-*', (route) => {
    const match = route.request().url().match(/\/api\/writer\/books\/(wb-[0-9a-z-]+)/);
    if (match) currentBookId = match[1];
    const method = route.request().method();
    if (method === 'DELETE') return route.fulfill(json({ ok: true }));
    if (method === 'PATCH') {
      const body = JSON.parse(route.request().postData() || '{}');
      if ('outline' in body) {
        return route.fulfill(json({ book: FAKE_BOOK_MANIFEST, outline_version_id: 'ol-new', synced_chapter_ids: [1, 2] }));
      }
      if ('setup' in body) recorded.setups.push(body.setup || {});
      return route.fulfill(json({ book: FAKE_BOOK_MANIFEST, setup: FAKE_SETUP, setup_version_id: 'su-new' }));
    }
    return route.fulfill(json(FAKE_BOOK_DETAIL()));
  });
  await page.route('**/api/writer/location', (route) => route.fulfill(json(FAKE_LOCATION)));
  await page.route('**/api/writer/reveal', (route) => route.fulfill(json({ ok: true, path: FAKE_LOCATION.writer_root })));
  await page.route('**/api/writer/generate/preview', (route) => route.fulfill(json({
    kind: 'genre', content: '【创作提示词】…', system: '系统提示词',
    budget: {
      included: [{ key: 'idea', label: '创作提示词', included: true, chars: 120 }],
      omitted: [], estimated_tokens: 72, context_known: true, fits: true,
      suggestions: [], chars: 120,
    },
  })));
  await page.route('**/api/writer/books', (route) => {
    const method = route.request().method();
    if (method === 'GET') {
      return route.fulfill(json({ books: [FAKE_BOOK_MANIFEST], corrupt: [] }));
    }
    if (method === 'POST') {
      recorded.creates += 1;
      const body = JSON.parse(route.request().postData() || '{}');
      return route.fulfill(json({
        book: { ...FAKE_BOOK_MANIFEST, id: 'wb-20261030-000000000002', title: body.title || '未命名作品', revision: 1, chapter_count: 0, setup_versions: [], outline_versions: [], active_pipeline: null },
      }), 201);
    }
    return route.fallback();
  });
  await page.route('**/api/writer/books/wb-*/export*', (route) => {
    recorded.exports += 1;
    return route.fulfill(json({
      format: 'txt', text: '第一章 归港的旧船\n\n正文。\n\n', missing_chapters: [2],
      filename: '雾港旧事.txt', book: FAKE_BOOK_MANIFEST, location: FAKE_LOCATION,
    }));
  });
  await page.route('**/api/writer/books/wb-*/pipeline', (route) => route.fulfill(json({ pipeline: currentPipeline() })));
  await page.route('**/api/writer/books/wb-*/pipelines', (route) => {
    recorded.pipelines.push(JSON.parse(route.request().postData() || '{}'));
    pipelineState = 'running';
    return route.fulfill(json({ pipeline: runningPipeline() }), 201);
  });
  await page.route('**/api/writer/books/wb-*/pipeline/cancel', (route) => {
    recorded.cancels += 1;
    pipelineState = 'failed';
    const pipeline = failedPipeline();
    pipeline.status = 'cancelled';
    pipeline.error = '用户取消';
    return route.fulfill(json({ pipeline }));
  });
  await page.route('**/api/writer/books/wb-*/pipeline/retry', (route) => {
    pipelineState = 'running';
    return route.fulfill(json({ pipeline: runningPipeline() }));
  });
  await page.route('**/api/writer/books/wb-*/versions/select', (route) => route.fulfill(json({ book: FAKE_BOOK_MANIFEST })));
  await page.route('**/api/writer/books/wb-*/sync', (route) => route.fulfill(json({ book: FAKE_BOOK_MANIFEST, chapter_ids: [1, 2] })));
  await page.route('**/api/writer/books/wb-*/chapters/*', (route) => {
    if (route.request().method() === 'PATCH') {
      return route.fulfill(json({ chapter_no: 1, version: { id: 'cv-new', version: 2, kind: 'manual', updated_at: 1750000700000, task_id: '', len: 100, finish_reason: '' }, duplicate: false, chapter_revision: 3 }), 201);
    }
    return route.fulfill(json(FAKE_CHAPTER_1));
  });
  // 导入两段式（multipart 不解析内容，只回预览 / 入库结果）。
  await page.route('**/api/writer/import', (route) => route.fulfill(json({
    import_id: IMPORT_ID, title: '雾港旧事（存档）', idea_len: 60, genre: '都市悬疑',
    character_count: 3, outline_count: 2, chapter_count: 1, sketch_only_count: 1,
    warnings: ['以下字段与本功能无关，未迁移：settings'], name: '作品存档.json',
  })));
  await page.route(`**/api/writer/import/${IMPORT_ID}/commit`, (route) => {
    recorded.commits += 1;
    return route.fulfill(json({
      book: { ...FAKE_BOOK_MANIFEST, id: 'wb-20261030-000000000003', title: '雾港旧事（存档）', revision: 1 },
      imported: { title: '雾港旧事（存档）' },
    }), 201);
  });
  // Studio 任务（writer 分区）：归属跟随 currentBookId（见上面兜底路由的说明）。
  const withCurrentBook = (task) => ({
    ...task,
    writer: { ...(task.writer || {}), book_id: currentBookId },
    input: { ...task.input, writer: { ...(task.input?.writer || {}), book_id: currentBookId } },
  });
  await page.route('**/api/studio/tasks?module=writer', (route) => route.fulfill(json({
    tasks: [withCurrentBook(FAKE_TASK), withCurrentBook(FAKE_WRITER_TASK_DONE)],
  })));
  await page.route(`**/api/studio/tasks/${TASK_ID}/events*`, (route) => route.fulfill(json({
    events: [
      { seq: 1, type: 'delta', content: '【题材推断】流式片段。' },
      { seq: 2, type: 'status', message: '模型输出中' },
    ],
    cursor: 2,
  })));
  await page.route(`**/api/studio/tasks/${TASK_ID}/cancel`, (route) => route.fulfill(json({ ok: true })));
  await page.route(`**/api/studio/tasks/${TASK_ID}`, (route) => route.fulfill(json({ task: FAKE_TASK })));
  await page.route('**/api/studio/tasks', (route) => {
    if (route.request().method() === 'DELETE') return route.fulfill(json({ ok: true, deleted: 1, deleted_ids: [], skipped: {} }));
    return route.fallback();
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForTimeout(2500);
    // 隔离实例是全新安装：首次启动引导会弹在 top layer 拦截点击，先关掉再继续。
    const onboardingOpen = await page.evaluate(() => document.querySelector('#onboardingDialog')?.open);
    if (onboardingOpen) {
      await page.click('#onboardingDialog [data-close="onboardingDialog"]');
      await page.waitForTimeout(400);
    }
    // 清掉上一轮视口留下的「最近打开作品」记录：桌面跑完会把它写进 localStorage，
    // 手机轮打开织书台时会自动去读这本旧书，与脚本随后新建作品的 openBook 竞态。
    await page.evaluate(() => { try { localStorage.removeItem('naibaWriterLastBookId'); } catch (_e) { /* ignore */ } });
    // 手机先进抽屉才点得到侧栏入口；桌面侧栏常驻。
    // 注意：上一轮桌面跑会把「工作台」视图偏好写进隔离实例的 settings，
    // 手机这轮冷启动就停在工作台态 —— 汉堡按钮要用工作台自己的 #studioOpenSidebar。
    const isMobile = vp.width <= 760;
    if (isMobile) {
      const state = await page.evaluate(() => ({
        open: document.querySelector('#sidebar')?.classList.contains('open'),
        studio: document.body.dataset.studio === 'on',
      }));
      if (!state.open) {
        await page.click(state.studio ? '#studioOpenSidebar' : '#openSidebar');
        await page.waitForTimeout(400);
      }
    }

    // 打开工作台（侧栏入口）
    await page.click('#openStudio');
    await page.waitForTimeout(800);
    if (isMobile) {
      // 进工作台后侧栏抽屉会自动收起；再开一次（工作台自己的汉堡按钮）。
      const drawerOpen = await page.evaluate(() => document.querySelector('#sidebar')?.classList.contains('open'));
      if (!drawerOpen) {
        await page.click('#studioOpenSidebar');
        await page.waitForTimeout(400);
      }
    }

    // 1) 导航：拆书匠改名 + 织书台第五页
    const navNames = await page.$$eval('#studioPages .studio-page span', (els) => els.map((el) => el.textContent.trim()));
    check('侧栏第五页按钮显示「织书台」，第四页显示「拆书匠」',
      navNames.includes('织书台') && navNames.includes('拆书匠') && !navNames.includes('创作场'),
      navNames.join('/'));
    await page.click('[data-studio-page="writer"]');
    await page.waitForTimeout(400);
    if (isMobile) {
      // 冷启动即工作台态时，抽屉是手动打开的，不会随切页自动收起；先关掉再点页面内的东西。
      // 抽屉盖住大半屏：点它右外侧露出的遮罩（中心点会被抽屉自身拦截）。
      const drawerOpen = await page.evaluate(() => document.querySelector('#sidebar')?.classList.contains('open'));
      if (drawerOpen) {
        await page.click('#sidebarBackdrop', { position: { x: 340, y: 300 } });
        await page.waitForTimeout(350);
      }
    }
    check('织书台页面可见', await page.isVisible('#studioPageWriter'));
    check('页面标题是「织书台」', (await page.textContent('#studioPageWriter h2')).trim() === '织书台');
    check('未打开作品时显示占位与空目录', await page.isVisible('#writerEditorPlaceholder'));

    // 2) 新建作品（走注入的 POST）
    await page.click('#writerNewBookBtn');
    await page.fill('#writerNewTitle', '雾港旧事');
    await page.click('#writerCreateBtn');
    await page.waitForTimeout(400);
    check('新建作品提交了 POST /api/writer/books', recorded.creates === 1);
    check('新建后页面状态显示作品名', (await page.textContent('#writerPageState')).includes('雾港旧事'));
    check('打开的作品在目录里显示章节', (await page.$$eval('#writerChapterList .writer-chapter-row', (els) => els.length)) === 2);

    // 3) 设定阶段：注入数据渲染 + 生成提交
    check('设定表单带出创意', (await page.inputValue('#writerIdea')).includes('修船工'));
    check('角色卡渲染 1 个角色', (await page.$$eval('#writerCharList .writer-char-card', (els) => els.length)) === 1);
    check('书名候选芯片可见', await page.isVisible('#writerTitleCandidates'));
    await page.click('#writerSetupGenerate');
    await page.waitForTimeout(500);
    check('生成整套设定提交 kind=setup_all（含角色数/章节数）',
      recorded.pipelines.length === 1 && recorded.pipelines[0].kind === 'setup_all'
      && Number(recorded.pipelines[0].char_count) === 4 && Number(recorded.pipelines[0].chapter_count) === 12,
      JSON.stringify(recorded.pipelines[0] || {}));
    // 生成进度区在右栏（手机上是「说明」档）：进度相关断言先切到那个档。
    const gotoNotesPane = async () => {
      if (!isMobile) return;
      const pane = await page.evaluate(() => document.querySelector('#writerColumns')?.dataset.writerPane);
      if (pane !== 'notes') {
        await page.click('[data-writer-pane="notes"]');
        await page.waitForTimeout(250);
      }
    };
    const backToEditorPane = async () => {
      if (!isMobile) return;
      await page.click('[data-writer-pane="editor"]');
      await page.waitForTimeout(250);
    };
    await gotoNotesPane();
    check('生成进度区可见且显示第 2/6 步',
      await page.isVisible('#writerPipelineBlock')
      && (await page.textContent('#writerPipelineStepsLabel')).includes('第 2 / 6 步'),
      await page.textContent('#writerPipelineStepsLabel'));
    // 流式片段靠 1.2s 周期的任务事件轮询带回来：等它，而不是赌固定延时。
    let streamShown = true;
    try {
      await page.waitForFunction(
        () => (document.querySelector('#writerPipelineStream')?.textContent || '').includes('流式片段'),
        { timeout: 6000 },
      );
    } catch { streamShown = false; }
    check('流式预览出现注入的事件片段', streamShown);

    // 4) 取消 → 失败态显示重试
    await page.click('#writerPipelineCancel');
    await page.waitForTimeout(400);
    check('取消后进度区显示重试按钮与错误', await page.isVisible('#writerPipelineRetry')
      && (await page.textContent('#writerPipelineError')).includes('用户取消'));
    pipelineState = 'none';
    await backToEditorPane();

    // 5) 大纲阶段
    await page.click('[data-writer-stage="outline"]');
    await page.waitForTimeout(300);
    check('大纲列表渲染 2 章', (await page.$$eval('#writerOutlineList .writer-outline-item', (els) => els.length)) === 2);
    check('大纲版本下拉有历史版本', (await page.$$eval('#writerOutlineVersion option', (els) => els.length)) >= 1);

    // 6) 正文阶段：点目录第一章 → 内容与版本（手机先切「作品」档才能点到目录）
    if (isMobile) {
      await page.click('[data-writer-pane="book"]');
      await page.waitForTimeout(250);
    }
    await page.click('#writerChapterList .writer-chapter-row');
    await page.waitForTimeout(400);
    if (isMobile) {
      await page.click('[data-writer-pane="editor"]');
      await page.waitForTimeout(250);
    }
    check('点目录切到正文并带出章节标题', (await page.textContent('#writerDraftTitle')).includes('归港的旧船'));
    check('正文内容渲染', (await page.inputValue('#writerDraftContent')).includes('四点四十分'));
    check('续写按钮可用（有正文），分段写可用', !(await page.isDisabled('#writerDraftContinue')));

    // 7) 模板弹窗：读真实 templates.json
    await page.click('[data-writer-stage="setup"]');
    await page.waitForTimeout(200);
    await page.click('#writerTemplateBtn');
    await page.waitForTimeout(600);
    const tplCount = await page.$$eval('#writerTplList .writer-tpl-card', (els) => els.length);
    check('模板弹窗渲染模板卡（真实 templates.json）', tplCount > 0, String(tplCount));
    await page.click('#writerTplList .writer-tpl-card');
    await page.waitForTimeout(200);
    check('点模板出详情与套用按钮', await page.isVisible('#writerTplDetail'));
    const ideaBefore = await page.inputValue('#writerIdea');
    await page.click('#writerTplApply');
    await page.waitForTimeout(300);
    check('套用模板把创意写进表单', (await page.inputValue('#writerIdea')) !== ideaBefore);

    // 7b) 套用模板后直接点生成：先把表单里的创意自动存成一版，再提交流水线
    //（真实反馈的回归锁：以前不先存，服务端拿空设定生成，只会回「未提供创意」）。
    const pipelinesBefore = recorded.pipelines.length;
    await page.click('#writerSetupGenerate');
    await page.waitForTimeout(700);
    check('套用模板后直接生成会先自动保存设定',
      recorded.setups.length === 1 && String(recorded.setups[0].idea || '').includes('写一部'),
      JSON.stringify(recorded.setups[0] || {}).slice(0, 140));
    check('自动保存后照常提交生成', recorded.pipelines.length === pipelinesBefore + 1);

    // 8) 导入存档（两段式）
    await page.click('#writerImportBtn');
    await page.waitForTimeout(300);
    const tmpJson = `${require('os').tmpdir()}/writer_smoke_archive_${Date.now()}.json`;
    require('fs').writeFileSync(tmpJson, JSON.stringify({ novel: {}, chapters: [] }));
    await page.setInputFiles('#writerImportFile', tmpJson);
    await page.waitForTimeout(500);
    check('导入预览弹窗出现且显示章节数', await page.isVisible('#writerImportDialog')
      && (await page.textContent('#writerImportPreview')).includes('正文章节'));
    await page.click('#writerImportCommit');
    await page.waitForTimeout(400);
    check('确认导入提交了 commit', recorded.commits === 1);

    // 9) 导出菜单
    await page.click('#writerExportBtn');
    await page.waitForTimeout(200);
    await page.click('[data-writer-export="txt"]');
    await page.waitForTimeout(400);
    check('导出 TXT 触发了一次导出请求', recorded.exports === 1);

    // 10) 手机分档：作品 / 编辑 / 说明 三档互斥
    if (vp.width <= 760) {
      await page.click('[data-writer-pane="book"]');
      await page.waitForTimeout(200);
      check('作品档：只有左栏可见', await page.isVisible('.writer-book') && !(await page.isVisible('.writer-editor')));
      await page.click('[data-writer-pane="editor"]');
      await page.waitForTimeout(200);
      check('编辑档：只有中栏可见', await page.isVisible('.writer-editor') && !(await page.isVisible('.writer-notes')));
      await page.click('[data-writer-pane="notes"]');
      await page.waitForTimeout(200);
      check('说明档：只有右栏可见', await page.isVisible('.writer-notes') && !(await page.isVisible('.writer-book')));
    } else {
      check('桌面三栏同显', await page.isVisible('.writer-book') && await page.isVisible('.writer-editor') && await page.isVisible('.writer-notes'));
    }

    // 11) 任务来源映射：模块库列表不出现 writer 任务（exclude_modules 分区），
    // 织书台管理记录里则显示 taskOrigin=织书台 的标题。
    await page.click('#writerManageBtn');
    await page.waitForTimeout(300);
    await page.click('[data-writer-manage-tab="task"]');
    await page.waitForTimeout(400);
    check('管理记录-生成记录显示本作品的任务', (await page.textContent('#writerManageList')).includes('题材推断'));

    for (const err of errors) checks.push(`console: ${err}`);
  } catch (e) {
    try {
      const shot = `${require('os').tmpdir()}/writer_smoke_fail_${vp.name}.png`;
      await page.screenshot({ path: shot });
      const state = await page.evaluate(() => ({
        stage: document.querySelector('[data-writer-stage].active')?.dataset.writerStage,
        panelHidden: document.querySelector('#writerSetupPanel')?.hidden,
        bookIdShown: document.querySelector('#writerPageState')?.textContent,
        placeholder: !document.querySelector('#writerEditorPlaceholder')?.hidden,
      }));
      checks.push(`exception: ${e.message} @ state=${JSON.stringify(state)} shot=${shot}`);
    } catch { checks.push(`exception: ${e.message}`); }
  } finally {
    await page.close();
  }

  function check(name, ok, detail = '') {
    checks.push(ok ? `✓ ${name}` : `✗ ${name}${detail ? ` —— ${detail}` : ''}`);
  }
  return { name: vp.name, failures: checks.filter((row) => !row.startsWith('✓')) , lines: checks };
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const results = [];
  try {
    for (const vp of VIEWPORTS) {
      results.push(await runViewport(browser, vp));
    }
  } finally {
    await browser.close();
  }
  let failed = false;
  for (const r of results) {
    if (r.failures.length) {
      failed = true;
      console.log(`[${r.name}] 失败项:`);
      for (const line of r.lines) console.log(`  ${line}`);
    } else {
      console.log(`[${r.name}] 全部通过（${r.lines.length} 项）`);
    }
  }
  process.exit(failed ? 1 : 0);
})();
