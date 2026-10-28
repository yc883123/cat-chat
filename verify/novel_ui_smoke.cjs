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
  { i: 1, title: '第1章 雨夜来客', len: 4860, manuscript_count: 1, adapted_count: 0,
    base_manuscript_id: 'ms-aaaaaaaaaaa1', base_kind: 'original', base_kind_label: '原文' },
  { i: 2, title: '第2章 灯下密信', len: 5120, manuscript_count: 1, adapted_count: 0,
    base_manuscript_id: 'ms-aaaaaaaaaaa2', base_kind: 'original', base_kind_label: '原文' },
  { i: 3, title: '第3章 不归桥', len: 4402, manuscript_count: 1, adapted_count: 0,
    base_manuscript_id: 'ms-aaaaaaaaaaa3', base_kind: 'original', base_kind_label: '原文' },
];
// 章节稿件：每章一份原文；保存编辑后追加改编稿（与服务端契约一致）。
const MANUSCRIPTS = {
  1: [{
    id: 'ms-aaaaaaaaaaa1', kind: 'original', kind_label: '原文', no: 0, label: '原文',
    len: 64, created_at: 1750000000000, source_manuscript_id: '', task_id: '', user_request: '',
    broken: false,
  }],
};
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
  base_manuscript_id: 'ms-aaaaaaaaaaa1',
  content_manuscript_id: 'ms-aaaaaaaaaaa1',
  manuscripts: [{
    id: 'ms-aaaaaaaaaaa1', kind: 'original', kind_label: '原文', no: 0, label: '原文',
    len: 44, created_at: 1750000000000, source_manuscript_id: '', task_id: '', user_request: '',
    broken: false,
  }],
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
// 单章 AI 润色的任务：结果直接是终态，用来验证「结果就地显示、按钮变保存改编稿」。
const ADAPT_TASK = {
  id: 'st-novel-adapt1', status: 'done', detail: '', error: '',
  title: '《长夜灯》· 单章改编', module: 'novel', origin_label: '创作场',
  result: 'AI 润色结果正文：他把伞收进门廊，雨水顺着袖口往下淌。', result_partial: '', usage: {},
  created_at: 1750000700000, updated_at: 1750000800000,
  novel: { book_id: BOOK_ID, stage: 'adapt', chapter_ids: [1] },
};
// 分镜生图（阶段四）任务：结果按「【第 N 场】」逐场一条，供逐场勾选与发送。
const STORYBOARD_TASK = {
  id: 'st-novel-story1', status: 'done', detail: '', error: '',
  title: '《长夜灯》· 分镜生图', module: 'novel', origin_label: '创作场',
  result: '【第 1 场】INT. 新场景 - 夜\n昏暗的老宅书房，油灯摇曳，一名书生俯身翻看桌上的信件，面部特写。\n\n【第 2 场】EXT. 长桥 - 黄昏\n夕阳把长桥染成金色，两名身影并肩走过桥面，远景剪影。',
  result_partial: '', usage: {},
  created_at: 1750000900000, updated_at: 1750001000000,
  novel: { book_id: BOOK_ID, stage: 'storyboard', scene_ids: [1] },
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
  // 页面内错误监听：Playwright 的 pageerror 不带文件名/行号，
  // 这里补一份，失败时能直接定位到前端哪一行。
  await page.addInitScript(() => {
    window.__novelErrors = [];
    window.addEventListener("error", (event) => {
      window.__novelErrors.push({
        message: event.message,
        file: String(event.filename || "").split("/").pop(),
        line: event.lineno,
        col: event.colno,
      });
    });
  });
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
  let previewSubmit = null;
  let recordsDelete = null;
  let tasksDelete = null;
  let revealCalled = false;
  // 章节标题与稿件一样是要能被 PATCH 改掉的（真实后端就是这样）：
  // mock 若永远回旧标题，前端「保存后列表标题同步」会被误判成失败。
  const CHAPTER_TITLES = Object.fromEntries(CHAPTERS.map((row) => [row.i, row.title]));

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
          body: JSON.stringify({ tasks: [ADAPT_TASK, stageTaskState] }),
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
      if (url.pathname.endsWith('st-novel-adapt1')) {
        return route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ task: ADAPT_TASK }),
        });
      }
      if (url.pathname.endsWith('st-novel-story1')) {
        return route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ task: STORYBOARD_TASK }),
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

  // 造梦机（ComfyUI 分页）最小 mock：分镜交接要切到该页并选一个带正向提示词
  // 的工作流。健康/任务/模型候选回空态即可，工作流回一份带正向提示词槽的假详情。
  await page.route((url) => url.pathname.startsWith('/api/comfyui/'), (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const json = (body, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: JSON.stringify(body),
    });
    if (path === '/api/comfyui/health') {
      return json({ ok: true, url: 'http://127.0.0.1:8188', version: 'smoke-0', devices: [{ name: 'cpu' }] });
    }
    if (path === '/api/comfyui/workflows' && request.method() === 'GET') {
      return json({ workflows: [{ id: 'wf-smoke-1', name: '文生图-冒烟', node_count: 3, param_count: 2 }] });
    }
    if (path === '/api/comfyui/workflows/wf-smoke-1' && request.method() === 'GET') {
      return json({
        workflow: {
          id: 'wf-smoke-1', name: '文生图-冒烟', node_count: 3,
          param_order: [],
          params: [
            { node_id: '6', field_name: 'text', field_type: 'string', role: 'positive_prompt', default: '', label: '6 · text' },
            { node_id: '3', field_name: 'seed', field_type: 'seed', default: 0, label: '3 · seed' },
          ],
        },
      });
    }
    if (path === '/api/comfyui/tasks') {
      return json({ tasks: [] });
    }
    if (path === '/api/comfyui/models') {
      return json({ models: [], loras: [], unets: [], clips: [], vaes: [], controlnets: [] });
    }
    return json({ error: `未注入的造梦机接口：${request.method()} ${path}` }, 404);
  });

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
      // 章节索引的标题要跟着 PATCH 更新（真实后端就是这样）：
      // mock 若固守旧标题，前端重新读作品后列表标题会被判成没同步。
      const chapters = CHAPTERS.map((row) => ({ ...row, title: CHAPTER_TITLES[row.i] || row.title }));
      return json({ book: BOOK, chapters, broken: [], location: 'X:/smoke/book' });
    }
    if (path === '/api/novel/location') {
      return json({
        data_dir: 'X:/smoke/data', novel_root: 'X:/smoke/data/studio/novel',
        can_open_folder: true, export_note: '导出由浏览器发起下载，通常保存到浏览器默认下载目录。',
      });
    }
    // 提交前预检：返回真实形态的预算报告（界面只用它渲染，不自己算）。
    if (path === '/api/novel/stage/preview' && method === 'POST') {
      previewSubmit = JSON.parse(request.postData() || '{}');
      const bodyChars = Number(previewSubmit.body_chars || 3000);
      return json({
        stage: previewSubmit.stage,
        limits: { body_chars: bodyChars, max_chapters: Number(previewSubmit.max_chapters || 6) },
        warnings: stageWarnings,
        chapters: (previewSubmit.chapter_ids || []).map((no) => ({
          i: no, title: CHAPTERS[no - 1]?.title || `第${no}章`,
          manuscript_id: (previewSubmit.manuscripts || {})[String(no)] || '',
          manuscript_label: (previewSubmit.manuscripts || {})[String(no)] ? '改编稿 1' : '原文',
          manuscript_kind: (previewSubmit.manuscripts || {})[String(no)] ? 'manual' : 'original',
          total_chars: 8000, used_chars: Math.min(8000, bodyChars),
          dropped_chars: Math.max(0, 8000 - bodyChars),
        })),
        sources: { chapters: [], elements_version: 0, outline_version: 0,
          uses_outline: false, uses_characters: false, has_summaries: false },
        budget: {
          chars: 9000, total_source_chars: 8000 * (previewSubmit.chapter_ids || []).length,
          used_chars: Math.min(8000, bodyChars) * (previewSubmit.chapter_ids || []).length,
          dropped_chars: Math.max(0, 8000 - bodyChars) * (previewSubmit.chapter_ids || []).length,
          estimated_tokens: 9200, estimate_note: '字数是字符数；token 为估算值，不等于实际用量',
          context_window: 128000, context_known: true, output_reserve: 4096,
          available_tokens: 123904, fits: true, suggestions: [],
          body_limit: bodyChars, max_chapters: Number(previewSubmit.max_chapters || 6),
          chapter_count: (previewSubmit.chapter_ids || []).length,
          kg_entities: 2, system_chars: 120, overhead_chars: 0,
        },
      });
    }
    // 管理记录清单
    if (path === `/api/novel/books/${BOOK_ID}/records` && method === 'GET') {
      return json({
        book: BOOK,
        chapters: CHAPTERS.map((row) => ({ i: row.i, title: row.title })),
        broken: [],
        manuscripts: Object.entries(MANUSCRIPTS).flatMap(([no, rows]) => rows.map((row) => ({
          ...row, chapter_no: Number(no),
          chapter_title: CHAPTERS[Number(no) - 1]?.title || '',
        }))),
        elements: [{ id: 'el-000000000001', version: 1, len: 40, updated_at: 1750000400000,
          task_id: 'old-elements', chapter_ids: [1], manuscripts: [], kind: 'elements' }],
        outlines: [{ id: 'ol-000000000001', version: 1, len: 20, updated_at: 1750000400000,
          task_id: 'old-outline', chapter_ids: [1], manuscripts: [], kind: 'outline' }],
        scripts: [{ id: 'sc-000000000001', version: 1, scene_count: 1, updated_at: 1750000400000,
          task_id: 'old-script', chapter_ids: [1], manuscripts: [], kind: 'script' }],
        location: 'X:/smoke/book',
      });
    }
    if (path === '/api/novel/records/delete' && method === 'POST') {
      recordsDelete = JSON.parse(request.postData() || '{}');
      return json({ ok: true, removed: recordsDelete.items || [], book: BOOK });
    }
    if (path === '/api/novel/tasks/delete' && method === 'POST') {
      tasksDelete = JSON.parse(request.postData() || '{}');
      return json({ deleted: tasksDelete.task_ids || [], skipped: [] });
    }
    if (path === '/api/novel/reveal' && method === 'POST') {
      revealCalled = true;
      return json({ ok: true, path: 'X:/smoke/data/studio/novel' });
    }
    if (path === `/api/novel/books/${BOOK_ID}/export`) {
      exportQueries.push(url.search);
      return json({ format: 'txt', text: '第1章 雨夜来客\n\n正文内容。\n' });
    }
    // 阶段二：生成提交（记录请求体，供断言任务契约字段）
    if (path === '/api/novel/stage' && method === 'POST') {
      stageSubmit = JSON.parse(request.postData() || '{}');
      if (stageSubmit.stage === 'adapt') {
        return json({ task: ADAPT_TASK, warnings: [], book_revision: BOOK.revision }, 201);
      }
      if (stageSubmit.stage === 'storyboard') {
        return json({
          task: { ...STORYBOARD_TASK, status: 'queued', result: '', result_partial: '' },
          warnings: [], book_revision: BOOK.revision,
          sources: { kg_entities: 2, scene_count: 1, chapters: [] },
        }, 201);
      }
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
        // 新增一份人工改编稿，原文保留（与后端同一契约）。
        const rows = MANUSCRIPTS[no] || [{ ...CHAPTER_BODY.manuscripts[0], id: `ms-aaaaaaaaaa${no}${no}` }];
        const created = {
          id: `ms-manual00000${no}`, kind: 'manual', kind_label: '人工改编稿',
          no: rows.filter((row) => row.kind === 'manual' || row.kind === 'ai').length + 1,
          label: '', len: String(savedBody.content || '').length,
          created_at: 1750000500000, source_manuscript_id: savedBody.base_manuscript_id || rows[0].id,
          task_id: '', user_request: '', broken: false,
        };
        created.label = `改编稿 ${created.no}`;
        const duplicate = rows.some((row) => row.content === savedBody.content
          && row.source_manuscript_id === created.source_manuscript_id);
        if (!duplicate) rows.push(created);
        MANUSCRIPTS[no] = rows;
        if (savedBody.title) CHAPTER_TITLES[no] = savedBody.title;
        return json({
          chapter: {
            i: no,
            title: CHAPTER_TITLES[no] ?? `第${no}章`,
            len: String(savedBody.content || '').length,
            revision: 2,
            base_manuscript_id: rows[0].id,
            manuscript_rows_ignored: true,
            manuscripts: rows.map((row) => ({ ...row })),
            created_manuscript: duplicate ? null : created,
          },
        });
      }
      if (method === 'DELETE') return json({ ok: true, chapter_count: 2 });
      const rows = MANUSCRIPTS[no] || CHAPTER_BODY.manuscripts;
      return json({
        ...CHAPTER_BODY,
        i: no,
        title: CHAPTER_TITLES[no] || CHAPTERS[no - 1]?.title || CHAPTER_BODY.title,
        base_manuscript_id: rows[0].id,
        content_manuscript_id: rows[0].id,
        manuscripts: rows.map((row) => ({ ...row })),
      });
    }
    const manuscriptMatch = path.match(new RegExp(`^/api/novel/books/${BOOK_ID}/chapters/(\\d+)/manuscripts/([^/]+)$`));
    if (manuscriptMatch) {
      const no = Number(manuscriptMatch[1]);
      const id = decodeURIComponent(manuscriptMatch[2]);
      if (method === 'POST') {
        const body = JSON.parse(request.postData() || '{}');
        const rows = MANUSCRIPTS[no] || CHAPTER_BODY.manuscripts;
        const created = {
          id: 'ms-ai0000000001', kind: body.kind || 'ai', kind_label: 'AI 改编稿', no: 2,
          label: '改编稿 2', len: String(body.content || '').length,
          created_at: 1750000600000, source_manuscript_id: body.source_manuscript_id || '',
          task_id: body.task_id || '', user_request: body.user_request || '', broken: false,
          content: body.content,
        };
        rows.push(created);
        MANUSCRIPTS[no] = rows;
        return json({ manuscript: created, duplicate: false, revision: 3,
          book: { ...BOOK, revision: (BOOK.revision || 0) + 1 } }, 201);
      }
      const row = (MANUSCRIPTS[no] || []).find((item) => item.id === id);
      return row ? json({ ...row, i: no }) : json({ error: '稿件不存在' }, 404);
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
  check('阶段导航五项', (await page.locator('#novelStages .novel-stage').count()) === 5);
  // 阶段二接入后「要素/大纲」可用；「剧本」属阶段三，「分镜」属阶段四，均已接入：
  // 五个阶段全部可用，没有禁用项。
  check('五个阶段全部可用',
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

  // ---- 4. 原文卡片与改编稿 ----
  const cards = page.locator('#novelManuscripts .novel-manuscript-card');
  check('打开章节先看到一张原文卡', (await cards.count()) === 1,
    String(await cards.count()));
  check('原文卡标出「原文」', (await cards.first().innerText()).includes('原文'));
  check('原文卡默认展开', await cards.first().locator('.novel-manuscript-body').isVisible());
  // 「正文紧不紧跟卡片标题」按卡片自身的标题栏量，而不是量到面板头——
  // 面板头下面还有章节标题、元信息与卡片操作行，那是必要信息，不是留白。
  const bodyTop = await page.evaluate(() => {
    const card = document.querySelector('#novelManuscripts .novel-manuscript-card');
    const body = card?.querySelector('.novel-manuscript-body');
    const head = card?.querySelector('.novel-manuscript-head');
    if (!body || !head) return null;
    return Math.round(body.getBoundingClientRect().top - head.getBoundingClientRect().bottom);
  });
  check('正文紧跟在卡片标题下方，没有大块留白',
    bodyTop !== null && bodyTop < 90, `间距 ${bodyTop}px`);
  check('编辑框默认不出现', await page.locator('#novelManuscriptEdit').isHidden());

  // 连续展开／收起三次，验证能反复折叠
  const toggle = cards.first().locator('[data-novel-manuscript-toggle]');
  for (let round = 1; round <= 3; round += 1) {
    await toggle.click();
    await page.waitForTimeout(100);
    const collapsed = !(await cards.first().locator('.novel-manuscript-body').isVisible());
    await toggle.click();
    await page.waitForTimeout(100);
    const expanded = await cards.first().locator('.novel-manuscript-body').isVisible();
    check(`第 ${round} 轮收起后能重新展开`, collapsed && expanded);
  }

  // 编辑 → 保存 → 新增改编稿，原文保留
  await cards.first().locator('[data-novel-manuscript-action="edit"]').click();
  await page.waitForTimeout(200);
  check('点编辑才出现输入框', await page.locator('#novelManuscriptEdit').isVisible());
  // 标题与正文都要在进入编辑态之后填：点「编辑」会重渲染一次，
  // 之前填的值会被覆盖（真实用户也是先点编辑再改内容）。
  await page.locator('#novelChapterTitle').fill('第1章 改过的标题');
  await page.locator('#novelChapterBody').fill('改过的正文内容。');
  await page.waitForTimeout(150);
  check('有未保存修改时保存按钮可用', !(await page.locator('#novelSaveChapter').isDisabled()));
  await page.locator('#novelSaveChapter').click();
  await page.waitForTimeout(1500);
  check('保存后状态回写为已保存',
    (await page.locator('#novelSaveState').innerText()) === '已保存',
    `${await page.locator('#novelSaveState').innerText()} / ${await page.locator('#novelSaveNote').innerText()} / ${JSON.stringify(savedBody)}`);
  check('保存请求带上 revision', savedBody && savedBody.revision === 1, JSON.stringify(savedBody));
  check('保存请求标明基于哪份稿件',
    Boolean(savedBody?.base_manuscript_id), JSON.stringify(savedBody));
  check('保存后多出一张改编稿卡', (await cards.count()) === 2, String(await cards.count()));
  check('原文卡仍在', (await cards.first().innerText()).includes('原文'));
  check('新卡标出改编稿 1',
    (await page.locator('#novelManuscripts').innerText()).includes('改编稿 1'));
  check('保存后列表标题同步', (await page.locator('#novelChapterList').innerText()).includes('改过的标题'),
    await page.locator('#novelChapterList').innerText());
  check('保存状态显示已保存', (await page.locator('#novelSaveState').innerText()) === '已保存');

  // 无改动保存不重复建卡：内容逐字相同时「保存改编稿」直接不可点，
  // 服务端也会按「同来源 + 同正文」判定重复，不会悄悄多出一张卡。
  await cards.nth(1).locator('[data-novel-manuscript-action="edit"]').click();
  await page.waitForTimeout(250);
  check('内容没改动时保存按钮不可点', await page.locator('#novelSaveChapter').isDisabled());
  check('没改动时不会多出卡片', (await cards.count()) === 2, String(await cards.count()));
  await page.locator('#novelRevertChapter').click();
  await page.waitForTimeout(250);
  check('取消编辑不创建记录', (await cards.count()) === 2, String(await cards.count()));

  // ---- 4b. 拆章阶段的 AI 润色：正文上限滑条也在润色面板里 ----
  // 滑条控制的是「单章改编时正文截取多少字」，拆章才是它的使用场景；阶段面板里的
  // 那份在拆章是整块隐藏的，所以润色面板带一份同一状态的滑条。
  await cards.first().locator('[data-novel-manuscript-action="ai"]').click();
  await page.waitForTimeout(300);
  check('AI 润色面板打开', await page.locator('#novelAiEdit').isVisible());
  check('拆章阶段也能看到正文上限滑条', await page.locator('#novelAiLimits').isVisible());
  const limitsHost = await page.evaluate(() => document.querySelector('#novelAiLimits')?.parentElement?.id || '');
  check('润色滑条在 AI 润色面板里', limitsHost === 'novelAiEdit', limitsHost);
  check('润色滑条带出当前正文上限',
    (await page.locator('#novelAiBodyCharsValue').innerText()) === '3000',
    await page.locator('#novelAiBodyCharsValue').innerText());
  await page.locator('#novelAiBodyChars').fill('4500');
  await page.locator('#novelAiBodyChars').dispatchEvent('input');
  await page.waitForTimeout(200);
  check('拆章滑条值实时更新', (await page.locator('#novelAiBodyCharsValue').innerText()) === '4500');
  if (SHOT_DIR) {
    await page.screenshot({ path: `${SHOT_DIR}/novel_split_ai_limits.png`, fullPage: false });
  }
  check('两份滑条共享同一个值',
    (await page.locator('#novelBodyChars').inputValue()) === '4500',
    await page.locator('#novelBodyChars').inputValue());
  // 还原默认值，后面的阶段断言按 3000 校验
  await page.locator('#novelAiBodyChars').fill('3000');
  await page.locator('#novelAiBodyChars').dispatchEvent('input');
  await page.waitForTimeout(150);

  // ---- 4c. 润色结果就地显示（不必先保存成稿件）----
  await page.fill('#novelAiRequest', '把这段写得更紧凑');
  await page.locator('#novelAiSubmit').click();
  await page.waitForTimeout(900);
  check('润色结果就地显示在面板里', await page.locator('#novelAiResult').isVisible());
  check('结果正文来自任务结果',
    (await page.locator('#novelAiResult').innerText()).includes('AI 润色结果正文'),
    await page.locator('#novelAiResult').innerText());
  check('有结果后按钮变成「保存改编稿」',
    (await page.locator('#novelAiSubmit').innerText()) === '保存改编稿',
    await page.locator('#novelAiSubmit').innerText());
  check('提示写明保存后才进作品',
    (await page.locator('#novelAiNote').innerText()).includes('保存改编稿'),
    await page.locator('#novelAiNote').innerText());
  if (SHOT_DIR) {
    await page.screenshot({ path: `${SHOT_DIR}/novel_ai_result.png`, fullPage: false });
  }
  await page.locator('#novelAiCancel').click();
  await page.waitForTimeout(250);
  check('关闭润色面板后滑条随之隐藏', await page.locator('#novelAiLimits').isHidden());

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

  // 已保存版本可反复折叠：标题即开关，收起后正文不占高度，再点回来。
  await page.locator('#novelStagePanel [data-novel-collapse="saved"]').click();
  await page.waitForTimeout(200);
  check('已保存版本可收起',
    await page.locator('#novelStageSavedBody').isHidden()
    && (await page.locator('#novelStagePanel [data-novel-collapse="saved"]').getAttribute('aria-expanded')) === 'false');
  await page.locator('#novelStagePanel [data-novel-collapse="saved"]').click();
  await page.waitForTimeout(200);
  check('已保存版本可再展开', await page.locator('#novelStageSavedBody').isVisible());

  // 分区：创作场只请求 module=novel
  check('创作场任务请求带 module=novel',
    studioTaskQueries.some((q) => q.includes('module=novel')), JSON.stringify(studioTaskQueries));
  check('创作场任务列表不含模块库任务',
    !(await page.locator('#novelStageResult').innerText()).includes('模板生成结果'));

  // ---- 7b-2. 双滑条：真实改变提交参数 ----
  check('每章字数滑条默认 3000',
    (await page.locator('#novelBodyChars').inputValue()) === '3000');
  await page.locator('#novelBodyChars').fill('6000');
  await page.locator('#novelBodyChars').dispatchEvent('input');
  await page.waitForTimeout(400);
  check('字数滑条值实时显示', (await page.locator('#novelBodyCharsValue').innerText()) === '6000');
  check('滑条值进入预检请求', previewSubmit && Number(previewSubmit.body_chars) === 6000,
    JSON.stringify(previewSubmit && previewSubmit.body_chars));
  check('右栏显示实际引用字数',
    (await page.locator('#novelPlanBody').innerText()).includes('发送正文'),
    await page.locator('#novelPlanBody').innerText());

  // 章数上限：可超过旧上限 6
  await page.locator('#novelMaxChapters').fill('7');
  await page.locator('#novelMaxChapters').dispatchEvent('input');
  await page.waitForTimeout(300);
  check('章数上限可以设为 7', (await page.locator('#novelMaxChaptersValue').innerText()) === '7');
  check('章数上限进入预检请求', previewSubmit && Number(previewSubmit.max_chapters) === 7,
    JSON.stringify(previewSubmit && previewSubmit.max_chapters));

  // ---- 7b-3. 逐章稿件选择 ----
  check('每个勾选章节都有稿件选择',
    (await page.locator('#novelPickManuscripts [data-novel-pick-manuscript]').count()) === 2,
    String(await page.locator('#novelPickManuscripts [data-novel-pick-manuscript]').count()));
  check('右栏逐章列出使用哪份稿件',
    (await page.locator('#novelPlanBody').innerText()).includes('本次素材'),
    await page.locator('#novelPlanBody').innerText());

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

  // 任务结果可反复折叠：收起后正文不占高度，再点回来（轮询只更新内容，不改折叠态）。
  await page.locator('#novelStagePanel [data-novel-collapse="task"]').click();
  await page.waitForTimeout(200);
  check('生成结果可收起',
    await page.locator('#novelStageResult').isHidden()
    && (await page.locator('#novelStagePanel [data-novel-collapse="task"]').getAttribute('aria-expanded')) === 'false');
  // 收起后卡片要按内容收拢，不能继续撑满剩余高度（会留一大块灰色空白）。
  const collapsedTaskH = await page.locator('#novelStageTask').evaluate((el) => Math.round(el.getBoundingClientRect().height));
  check('收起后任务卡不留空白', collapsedTaskH < 100, String(collapsedTaskH));
  if (SHOT_DIR) {
    await page.screenshot({ path: `${SHOT_DIR}/novel_collapsed_result.png`, fullPage: false });
  }
  await page.locator('#novelStagePanel [data-novel-collapse="task"]').click();
  await page.waitForTimeout(200);
  check('生成结果可再展开', await page.locator('#novelStageResult').isVisible());
  const expandedTaskH = await page.locator('#novelStageTask').evaluate((el) => Math.round(el.getBoundingClientRect().height));
  check('展开后任务卡重新铺满', expandedTaskH > collapsedTaskH + 60,
    `${collapsedTaskH} → ${expandedTaskH}`);

  // 结果很长时必须由「编辑区整体滚动」承接，不能让结果正文溢出任务区压到场次区上。
  const resultLayout = await page.evaluate(() => {
    const pre = document.querySelector('#novelStageResult');
    if (!pre) return null;
    const keep = pre.textContent;
    pre.textContent = ('很长的剧本文本。'.repeat(40) + String.fromCharCode(10)).repeat(30);
    const task = document.querySelector('#novelStageTask').getBoundingClientRect();
    const preRect = pre.getBoundingClientRect();
    const scene = document.querySelector('#novelSceneBlock').getBoundingClientRect();
    pre.textContent = keep;
    return {
      preOverTask: Math.round(preRect.bottom - task.bottom),
      preOverScene: Math.round(preRect.bottom - scene.top),
    };
  });
  check('长结果不溢出任务区、不压到场次区',
    Boolean(resultLayout) && resultLayout.preOverTask <= 0 && resultLayout.preOverScene <= 0,
    JSON.stringify(resultLayout));

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

  // ---- 8b. 阶段四：分镜生图（逐场提示词 + 交接造梦机） ----
  await page.locator('#novelStages [data-novel-stage="storyboard"]').click();
  await page.waitForTimeout(400);
  check('切到分镜阶段', (await page.locator('#novelEditorHead').innerText()) === '分镜生图');
  check('分镜用独立生成面板', await page.locator('[data-stage-panel="storyboard"]#novelStagePanel').count() === 1);
  check('分镜面板不显示逐章选稿', await page.locator('#novelPickManuscripts').isHidden());
  check('分镜面板不显示正文上限滑条',
    (await page.locator('#novelStagePanel .novel-limits').isHidden()) === true);
  check('生成分镜提示词按钮存在',
    (await page.locator('#novelStageGenerate').innerText()) === '生成分镜提示词');
  check('有场次时分镜说明给出场次计数',
    (await page.locator('#novelStoryboardNote').innerText()).includes('共 1 场'),
    await page.locator('#novelStoryboardNote').innerText());
  check('分镜阶段没有写回按钮', await page.locator('#novelStageApply').isHidden());
  // 写法（系统提示词）：可编辑、随提交发送、可一键恢复默认。
  check('写法编辑框可见', await page.locator('#novelStoryboardSystem').isVisible());
  check('无自定义写法时恢复按钮隐藏', await page.locator('#novelStoryboardReset').isHidden());
  await page.locator('#novelStoryboardSystem')
    .fill('自定义模板写法：每场一条，水彩风格，画面描述按镜头语言组织。');
  await page.waitForTimeout(300);
  await page.locator('#novelStageGenerate').click();
  await page.waitForTimeout(500);
  check('分镜提交带 stage=storyboard 与场次选择',
    stageSubmit && stageSubmit.stage === 'storyboard' && Array.isArray(stageSubmit.scene_ids),
    JSON.stringify(stageSubmit));
  check('分镜提交带自定义写法',
    stageSubmit && stageSubmit.system_prompt === '自定义模板写法：每场一条，水彩风格，画面描述按镜头语言组织。',
    JSON.stringify(stageSubmit && stageSubmit.system_prompt));
  check('分镜提交不要求勾选章节', stageSubmit && !Array.isArray(stageSubmit.chapter_ids),
    JSON.stringify(stageSubmit));
  // 轮询走到 done 后逐场行渲染；mock 的结果有两场，全部默认勾选。
  await page.waitForFunction(
    () => document.querySelectorAll('#novelStoryboardList .novel-storyboard-row').length >= 2,
    null, { timeout: 6000 },
  );
  check('分镜结果逐场显示', (await page.locator('.novel-storyboard-row').count()) === 2);
  check('逐场行带场景行文本',
    (await page.locator('.novel-storyboard-row').first().innerText()).includes('INT. 新场景 - 夜'));
  check('默认全部勾选', (await page.locator('[data-novel-storyboard-row]:checked').count()) === 2);
  check('发送按钮带条数',
    (await page.locator('#novelStoryboardSend').innerText()).includes('（2 条）'));
  // 取消勾选第二场：按钮条数同步；再全选回来。
  await page.locator('[data-novel-storyboard-row]').nth(1).uncheck();
  await page.waitForTimeout(150);
  check('取消勾选后条数同步',
    (await page.locator('#novelStoryboardSend').innerText()).includes('（1 条）'));
  await page.locator('#novelStoryboardAll').click();
  await page.waitForTimeout(150);
  check('全选恢复两条',
    (await page.locator('#novelStoryboardSend').innerText()).includes('（2 条）'));
  // 发送到造梦机：切页 + 队列条出现。
  await page.locator('#novelStoryboardSend').click();
  await page.waitForTimeout(700);
  check('发送后切到造梦机页', await page.locator('#studioPageComfyui').isVisible());
  check('分镜队列条出现', await page.locator('#comfyuiStoryboard').isVisible());
  check('队列条带两条提示词',
    (await page.locator('.comfyui-storyboard-item').count()) === 2);
  check('队列条带场次说明',
    (await page.locator('.comfyui-storyboard-item').first().innerText()).includes('第 1 场'));
  // 选工作流后填入：正向提示词框拿到第一条的内容。
  await page.locator('.comfyui-workflow-card .comfyui-workflow-title').first().click();
  await page.waitForTimeout(500);
  const promptArea = page.locator('[data-comfyui-slot="6|text"]');
  check('工作流参数表单出现正向提示词框', (await promptArea.count()) === 1);
  await page.locator('.comfyui-storyboard-item .control-button').first().click();
  await page.waitForTimeout(250);
  check('填入把提示词写进正向提示词框',
    (await promptArea.inputValue()).includes('昏暗的老宅书房'),
    await promptArea.inputValue());
  check('填过后条目标记已填',
    (await page.locator('.comfyui-storyboard-item.filled').count()) === 1);
  await page.locator('#comfyuiStoryboardClear').click();
  await page.waitForTimeout(200);
  check('清空后队列条隐藏', await page.locator('#comfyuiStoryboard').isHidden());
  // 回创作场：写法还在，可一键恢复默认。
  await page.locator('#studioPages [data-studio-page="novel"]').click();
  await page.waitForTimeout(600);
  check('回到创作场后写法仍在',
    (await page.locator('#novelStoryboardSystem').inputValue()).includes('自定义模板写法'),
    await page.locator('#novelStoryboardSystem').inputValue());
  await page.locator('#novelStoryboardReset').click();
  await page.waitForTimeout(250);
  check('恢复默认写法清空编辑框', (await page.locator('#novelStoryboardSystem').inputValue()) === '');
  check('清空后恢复按钮隐藏', await page.locator('#novelStoryboardReset').isHidden());

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

  // ---- 7e. 管理记录 ----
  check('阶段栏最右侧有管理记录入口', await page.locator('#novelManageBtn').isVisible());
  await page.locator('#novelManageBtn').click();
  await page.waitForTimeout(700);
  check('管理记录面板打开', await page.locator('#novelManageDialog').isVisible());
  check('管理记录标明只管理当前作品',
    (await page.locator('#novelManageScope').innerText()).includes('长夜灯'));
  check('章节稿件页签有记录',
    (await page.locator('#novelManageList .novel-manage-row').count()) >= 2,
    `${await page.locator('#novelManageList .novel-manage-row').count()} 行 / 错误「${await page.locator('#novelManageError').innerText()}」/ 列表「${await page.locator('#novelManageList').innerText()}」`);

  // 点一行就地看内容：稿件读正文、要素读对应版本、生成记录读任务结果。
  await page.locator('#novelManageList [data-novel-manage-open]').first().click();
  await page.waitForTimeout(500);
  check('点记录立即就地显示内容', await page.locator('#novelManagePreview').isVisible());
  const previewText = await page.locator('#novelManagePreviewBody').innerText();
  check('稿件记录读到的是正文', previewText.length > 8 && !previewText.includes('正在读取'),
    previewText.slice(0, 60));
  if (SHOT_DIR) {
    await page.screenshot({ path: `${SHOT_DIR}/novel_manage_preview.png`, fullPage: false });
  }
  await page.locator('#novelManageTabs [data-novel-manage-tab="elements"]').click();
  await page.waitForTimeout(300);
  check('要素页签显示要素版本',
    (await page.locator('#novelManageList').innerText()).includes('要素 第 1 版'),
    await page.locator('#novelManageList').innerText());
  await page.locator('#novelManageList [data-novel-manage-open]').first().click();
  await page.waitForTimeout(400);
  check('要素记录读到对应版本内容',
    (await page.locator('#novelManagePreviewBody').innerText()).includes('旧版人物'),
    await page.locator('#novelManagePreviewBody').innerText());
  await page.locator('#novelManageTabs [data-novel-manage-tab="task"]').click();
  await page.waitForTimeout(300);
  check('生成记录页签列出任务',
    (await page.locator('#novelManageList').innerText()).includes('长夜灯'),
    await page.locator('#novelManageList').innerText());
  await page.locator('#novelManageList [data-novel-manage-open]').first().click();
  await page.waitForTimeout(400);
  check('生成记录读到任务结果',
    (await page.locator('#novelManagePreviewBody').innerText()).includes('AI 润色结果正文'),
    await page.locator('#novelManagePreviewBody').innerText());
  await page.locator('#novelManageTabs [data-novel-manage-tab="manuscript"]').click();
  await page.waitForTimeout(300);
  // 多选 + 全选当前筛选 + 删除（确认框里写明范围）
  await page.locator('#novelManageSelectAll').click();
  await page.waitForTimeout(300);
  check('全选当前筛选有明确数量',
    (await page.locator('#novelManageSelection').innerText()).includes('已选'),
    await page.locator('#novelManageSelection').innerText());
  check('全选后可删除',
    !(await page.locator('#novelManageDelete').isDisabled()),
    `${await page.locator('#novelManageSelection').innerText()} / 行数 ${await page.locator('#novelManageList .novel-manage-row').count()} / ${await page.locator('#novelManageList').innerText()}`);
  const confirmTexts = [];
  page.on('dialog', (dialog) => { confirmTexts.push(dialog.message()); dialog.accept(); });
  await page.locator('#novelManageDelete').click();
  await page.waitForTimeout(900);
  check('删除请求带上选中记录', Boolean(recordsDelete) && (recordsDelete.items || []).length > 0,
    JSON.stringify(recordsDelete));
  check('删除确认说明不可撤销',
    confirmTexts.some((text) => text.includes('不能撤销')), JSON.stringify(confirmTexts));
  check('删除确认说明不影响原文',
    confirmTexts.some((text) => text.includes('不会删除原文')), JSON.stringify(confirmTexts));
  await page.locator('#novelManageClose').click();
  await page.waitForTimeout(300);
  check('管理记录关闭回到原阶段', !(await page.locator('#novelManageDialog').isVisible()));

  // ---- 7f. 保存位置与导出说明 ----
  const locationText = await page.locator('#novelLocationPath').innerText();
  check('保存位置显示后端解析路径', locationText.includes('studio') && locationText.includes('novel'),
    locationText);
  check('保存位置可复制', await page.locator('#novelCopyPath').isVisible());
  check('导出菜单说明下载去向',
    (await page.locator('#novelExportNote').innerText()).includes('浏览器默认下载目录'),
    await page.locator('#novelExportNote').innerText());

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

  // ---- 11. 切页不丢状态 + 提词库「发送到分镜生图」交接 ----
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(300);
  await page.locator('#studioPages [data-studio-page="library"]').click();
  await page.waitForTimeout(700);
  // 选中一条模板，把正文发到分镜生图当写法：应切到创作场分镜阶段且编辑框已带入。
  await page.locator('#studioList .studio-row').first().click();
  await page.waitForTimeout(700);
  check('提词库详情出现发送到分镜生图按钮', await page.locator('#studioSendStoryboard').isVisible());
  await page.locator('#studioSendStoryboard').click();
  await page.waitForTimeout(700);
  check('发送后切到创作场', await page.locator('#studioPageNovel').isVisible());
  check('发送后落在分镜阶段', (await page.locator('#novelEditorHead').innerText()) === '分镜生图');
  check('模板正文进入写法编辑框',
    (await page.locator('#novelStoryboardSystem').inputValue()).length > 10,
    await page.locator('#novelStoryboardSystem').inputValue());
  check('导入后出现恢复默认按钮', !(await page.locator('#novelStoryboardReset').isHidden()));
  await page.locator('#novelStoryboardReset').click();
  await page.waitForTimeout(250);
  check('导入的写法可一键恢复默认', (await page.locator('#novelStoryboardSystem').inputValue()) === '');
  await page.locator('#studioPages [data-studio-page="novel"]').click();
  await page.waitForTimeout(400);
  check('切页返回后作品仍在', (await page.locator('#novelBookTitle').innerText()).includes('长夜灯'));

  // 收尾：把「上次所在页」还原成提词库。功能页偏好落在服务端配置里，
  // 留在 novel 会让后续 studio_ui_smoke 从创作场起步而大面积误报。
  await page.locator('#studioPages [data-studio-page="library"]').click();
  await page.waitForTimeout(500);

  // ---- 12. 无 pageerror / console.error ----
  const realErrors = errors.filter((line) => !/favicon|ERR_INTERNET|net::ERR/.test(line));
  const pageErrors = await page.evaluate(() => window.__novelErrors || []);
  check('无 pageerror / console.error', realErrors.length === 0,
    `${realErrors.join(' | ')} || 页面内明细：${JSON.stringify(pageErrors)}`);

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
