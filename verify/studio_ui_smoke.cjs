// 工作台（Studio）前端冒烟：桌面 + 375px 手机视口。
// 断言：侧栏「工作台」是唯一入口、重复点不退出；纵向分类带计数；列表可搜索；
// 选中条目出现详情/变量/正文预览；「我的输入」是一级输入位；
// 侧栏「历史产出」打开完整列表（0/1/3/60 条口径、字段回退）、点行显示结果；
// 任务动作在结果档可用；手机三档按定义切换；追问框初始无选中且「创建会话并继续」禁用、
// 取消只关闭不创建会话；无 pageerror / console.error。
// 另含：桌面侧栏收起后的恢复入口、任务/自定义条目迟到响应、离开期间任务终结后重进恢复、
// 搜索过滤、自由输入生成、取消接口契约。
// 交接闭环（30 节，修复 3.6.1-beta「点确认什么都没发生」的回归守门）：
// 确认真调 POST（恰一次/忙碌期重复触发不重复/完成前关弹窗不再加请求）、
// 4xx 与缺 conversation_id 都留在工作台、会话详情读取失败不重发交接、
// 成功后真打开注入的普通会话（弹窗关、工作台退场、消息区渲染，不等 Run 完成）。
// 目录/任务/Agent/生成走路由注入（隔离装的模型目录连不出去）：不真连供应商、不真创建会话。
// 运行（先起隔离源码实例 verify\_serve_tmp.py）：node verify\studio_ui_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';

const TASK_ID = 'st-smoke-0001';
const FAKE_TASK = {
  id: TASK_ID, status: 'done', detail: '', error: '',
  title: '冒烟任务', module: 'global', origin_label: '', result: '这是一段冒烟生成的结果正文。',
  result_partial: '', reasoning: [], usage: {}, performance: {}, finish_reason: '',
  handoff: null, created_at: 1750000000000, updated_at: 1750000001000, started_at: 1750000000000, finished_at: 1750000001000,
  input: { content: '冒烟输入', model_key: 'online:probe-te', model_name: 'probe-default', item_id: '', title: '冒烟任务' },
};
// 字段缺失的任务：标题 / 时间 / 来源都必须走回退文案，不能露空或显示英文状态码。
const FAKE_TASKS = [
  FAKE_TASK,
  { id: 'st-smoke-0002', status: 'running', title: '', module: 'vs', origin_label: '', created_at: null, result: '', result_partial: '正在产出的前半段正文' },
  { id: 'st-smoke-0003', status: 'failed', title: '失败任务', module: 'vs', origin_label: '文生图 · 自建模板', created_at: 1750000002000, result: '', error: '模型超时' },
  { id: 'st-smoke-0004', status: 'interrupted', title: '中断任务', module: 'script', origin_label: '', created_at: 'bad-value', result: '已生成的部分' },
  // 全字段缺失：标题 →「未命名任务」，来源 →「来源未记录」，时间 →「时间未记录」
  { id: 'st-smoke-0005', status: 'cancelled', title: '', module: '', origin_label: '', created_at: 0, result: '', result_partial: '' },
  // 第二条已完成结果：用于「快速切 A→B，A 的迟到响应不覆盖 B」的对照样本
  { id: 'st-smoke-0006', status: 'done', title: '第二条结果', module: 'global', origin_label: '', created_at: 1750000003000, result: '第二条结果正文，用于迟到响应保护校验。', result_partial: '' },
];
const FAKE_AGENTS = [
  { id: 'agent-smoke-a', name: '冒烟 Agent A', system_prompt: '假系统提示词（只选一个）' },
  { id: 'agent-smoke-b', name: '冒烟 Agent B', system_prompt: '' },
];
const FAKE_CUSTOM = [
  { id: 'cu-smoke-a', title: '自定义条目 A', summary: 'A 摘要', type: 'template', module: 'global', tags: [], genre: '', chars: 5, vars: [], body: '自定义条目 A 的正文。', created_at: 1, updated_at: 1 },
];

const HANDOFF_CONV_ID = 'conv-smoke-handoff-1';
const OPENFAIL_CONV_ID = 'conv-smoke-openfail-1';
// 交接成功后 openConversation 读取的会话详情（messages/行形状同 storage._message_dict）。
const FAKE_HANDOFF_CONVERSATION = {
  id: HANDOFF_CONV_ID, title: '工作台·冒烟任务', conv_kind: 'main', mode: 'online', permission_mode: 'confirm',
  web_search_enabled: 0, deep_reasoning_enabled: 0, lightweight_mode: 0, lightweight_disabled_features: '[]',
  title_customized: 1, system_prompt: '', stream_enabled: 1, workspace_dir: '', reasoning_effort: 'off',
  enabled_tool_ids: '', agent_id: 'agent-smoke-a', model_key: 'online:probe-te', model_name: 'probe-default',
  created_at: 1750000010000, updated_at: 1750000010000,
  messages: [{
    id: 'msg-smoke-hand-1', conversation_id: HANDOFF_CONV_ID, role: 'user', metadata: {}, created_at: 1750000010000,
    content: '【来自 Cat Chat 工作台的交接】\n- 我的输入：冒烟输入\n- 工作台生成结果：\n<<<\n这是一段冒烟生成的结果正文。\n>>>',
  }],
};

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'desktop-wide', width: 1440, height: 900 },
  { name: 'mobile', width: 375, height: 812 },
  { name: 'mobile-390', width: 390, height: 844 },
];

function boundaryTasks(count) {
  return Array.from({ length: count }, (_, index) => ({
    id: `st-bound-${index}`, status: 'done', title: `边界任务 ${index}`, module: 'global',
    origin_label: '', created_at: 1750000000000 + index, result: `边界结果 ${index}`,
    result_partial: '', error: '', usage: {}, handoff: null,
  }));
}

// 每个视口用独立副本：测试会就地改写任务状态（运行中 → 已完成等）。
function freshTaskList() {
  return FAKE_TASKS.map((task) => ({ ...task }));
}

async function runViewport(browser, vp) {
  const errors = [];
  const handoffCalls = [];
  // 任务列表可切换成「失败一次再成功」，用于验证重试入口；
  // taskList 同时控制注入条数（0 / 1 / 3 / 6 / 60）。
  let taskList = freshTaskList();
  // 预期中的 500：只豁免真正注入到 /api/studio/tasks 的那几次，其他 500 一律报错。
  let expectedConsole500 = 0;
  let historyFailuresLeft = 0;
  // 轮询状态与计数：验证「历史列表不被轮询覆盖」和「离开工作台停止轮询」。
  let eventStatus = 'done';
  let eventRequests = 0;
  let cancelCalls = 0;
  const generateCalls = [];
  const conversationGetCalls = [];
  let conversationGetFails = 0;
  // 交接接口注入状态机：forbid=默认熔断（点亮「不应触达」）、http-error=撤出真实 4xx、
  // missing-id=200 但缺 conversation_id、ok-open-fail=返回 id 但会话详情读取 500、ok=成功返回。
  let handoffBehavior = 'forbid';
  let handoffDelayMs = 0;
  // 注入预期的资源加载失败（端点+状态码配对豁免一次；console 顺序不定，不能按个数判）。
  const routeErrorExemptions = [];
  // 迟到响应保护：这些任务 id 的详情接口延迟返回，用于验证快速切换不串台。
  const slowDetailMs = new Map();
  // 自定义条目列表接口的延迟（毫秒）：用于验证自定义条目迟到加载不覆盖新选择。
  let customDelayMs = 0;
  // ---- 工作台偏好 / 收藏 / 图片（31 节起）----
  // settings.studio 的测试态：POST 被拦截后只写这份内存对象，不污染隔离实例的 config。
  let studioPrefsState = { last_selection: null, favorites: [] };
  const settingsPosts = [];
  let uploadCounter = 0;
  let uploadFailuresLeft = 0;
  let uploadDelayMs = 0;
  let visionMode = 'ok'; // ok | partial | fail
  const visionCalls = [];
  const PIXEL_PNG = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
    'base64',
  );
  const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    const url = msg.location()?.url || '';
    // 隔离实例的供应商目录指向 example.invalid，模型目录探测 400 是本次冒烟的预期噪声
    // （不评模型能力，只验证 UI 结构与交互），不计为页面错误。
    if (text.includes('status of 400 (Bad Request)') && url.includes('/api/providers/models')) return;
    // 只豁免「精确端点 + 精确注入次数」的 500：其他接口/其他次数的 500 必须报错。
    if (text.includes('status of 500 (Internal Server Error)')) {
      let pathname = '';
      try { pathname = new URL(url).pathname; } catch { pathname = ''; }
      if (pathname === '/api/studio/tasks' && expectedConsole500 > 0) {
        expectedConsole500 -= 1;
        return;
      }
    }
    // 交接闭环注入的预期失败：按端点 + 状态码配对豁免一次，其余照旧报错。
    const injectedStatus = text.match(/status of (\d+) \(/);
    if (injectedStatus) {
      let injectedPath = '';
      try { injectedPath = new URL(url).pathname; } catch { injectedPath = ''; }
      const idx = routeErrorExemptions.findIndex((row) => row.path === injectedPath && row.status === Number(injectedStatus[1]));
      if (idx >= 0) {
        routeErrorExemptions.splice(idx, 1);
        return;
      }
    }
    errors.push(`console.error: ${text}${url ? ` @ ${url}` : ''}`);
  });
  // 任务/Agent/交接/生成注入（隔离实例天然没有已完成的生成任务）。
  await page.route('**/api/studio/tasks', (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    if (historyFailuresLeft > 0) {
      historyFailuresLeft -= 1;
      expectedConsole500 += 1;
      return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: '冒烟注入：历史读取失败' }) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ tasks: taskList }) });
  });
  await page.route('**/api/studio/tasks/st-*', (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const id = route.request().url().split('/').pop().split('?')[0];
    // 详情接口默认立即返回；打上慢标记时延迟，用于验证迟到响应不串台。
    const respond = () => {
      const task = taskList.find((row) => row.id === id) || FAKE_TASK;
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ task }) });
    };
    const delay = slowDetailMs.get(id) || 0;
    if (delay) setTimeout(() => { void respond(); }, delay);
    else void respond();
    return undefined;
  });
  await page.route('**/api/studio/tasks/*/events*', (route) => {
    eventRequests += 1;
    const terminal = eventStatus !== 'running' && eventStatus !== 'queued';
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        events: terminal ? [{ seq: 1, type: 'done' }] : [{ seq: 1, type: 'delta', content: '增量' }],
        count: 1, after: 0, last_seq: 1, terminal, terminal_seq: terminal ? 1 : 0, status: eventStatus,
      }),
    });
  });
  // 真实契约：取消成功返回 { ok: true }（不是任务快照）。
  await page.route('**/api/studio/tasks/*/cancel', (route) => {
    cancelCalls += 1;
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
  });
  await page.route('**/api/studio/generate', (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    generateCalls.push(body);
    const task = {
      ...FAKE_TASK, id: 'st-smoke-gen-1', status: 'done', title: '生成任务',
      result: '生成结果正文：冒烟生成完成。', result_partial: '', created_at: 1750000009000,
    };
    taskList = [task, ...taskList.filter((row) => row.id !== task.id)];
    return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ task, unresolved_vars: [] }) });
  });
  await page.route('**/api/studio/custom', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    if (customDelayMs) await new Promise((resolve) => setTimeout(resolve, customDelayMs));
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: FAKE_CUSTOM, corrupt: [] }) });
  });
  await page.route('**/api/studio/custom/*', (route) => {
    const method = route.request().method();
    if (method === 'PATCH') {
      const body = JSON.parse(route.request().postData() || '{}');
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ item: { ...FAKE_CUSTOM[0], body: body.body || FAKE_CUSTOM[0].body } }) });
    }
    return route.fallback();
  });
  await page.route('**/api/agents', (route) => {
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ agents: FAKE_AGENTS, default_agent_id: '' }) });
  });
  await page.route('**/api/studio/handoff', (route) => {
    handoffCalls.push(JSON.parse(route.request().postData() || '{}'));
    const respond = () => {
      if (handoffBehavior === 'ok') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ conversation_id: HANDOFF_CONV_ID, run_id: 'run-smoke-1' }) });
      }
      if (handoffBehavior === 'missing-id') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ run_id: 'run-smoke-none' }) });
      }
      if (handoffBehavior === 'ok-open-fail') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ conversation_id: OPENFAIL_CONV_ID, run_id: 'run-smoke-2' }) });
      }
      if (handoffBehavior === 'http-error') {
        routeErrorExemptions.push({ path: '/api/studio/handoff', status: 400 });
        return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: '冒烟注入：交接 4xx（演练）' }) });
      }
      return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: '冒烟注入：不应触达交接接口' }) });
    };
    if (handoffDelayMs) setTimeout(() => { void respond(); }, handoffDelayMs);
    else { void respond(); }
  });
  // 交接成功/打开失败两份注入会话：openConversation 读详情 / first_turn / 轮询恢复。
  await page.route(`**/api/conversations/${HANDOFF_CONV_ID}`, (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    conversationGetCalls.push(route.request().url());
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FAKE_HANDOFF_CONVERSATION) });
  });
  await page.route(`**/api/conversations/${HANDOFF_CONV_ID}/first_turn`, (route) => {
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
  });
  await page.route(`**/api/conversations/${OPENFAIL_CONV_ID}`, (route) => {
    routeErrorExemptions.push({ path: `/api/conversations/${OPENFAIL_CONV_ID}`, status: 500 });
    conversationGetFails += 1;
    return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: '冒烟注入：会话读取失败' }) });
  });
  // 隔离实例的供应商目录指向 example.invalid：模型目录返回固定列表，
  // 让「识别图片」请求能带上具体模型名（真实环境里由供应商 /models 返回）。
  await page.route('**/api/providers/models', (route) => {
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ models: ['probe-default'] }),
    });
  });
  // 模型档案夹具：「识别图片」断言期望请求携带 online:probe-te，但隔离实例的 config
  // 由各次冒烟自行准备，唯一模型不一定是它（2026-10-25 评审复跑时是 online:review-smoke，
  // 四视口同断言失败）。这里改写 bootstrap 的模型档案为固定一份 + 同步默认模型，
  // 冒烟不再依赖实例配置怎么准备；模型名由上面的 /api/providers/models mock 配对提供。
  await page.route('**/api/bootstrap', async (route) => {
    const response = await route.fetch();
    let body;
    try { body = await response.json(); } catch { return route.fulfill({ response }); }
    const key = 'online:probe-te';
    const profiles = Array.isArray(body.model_profiles) && body.model_profiles.length
      ? body.model_profiles
      : (Array.isArray(body.providers) ? body.providers : []);
    const fake = {
      ...(profiles.length ? { ...profiles[0] } : { kind: 'online' }),
      model_key: key,
      name: 'probe-te 冒烟模型',
      kind: (profiles[0] && profiles[0].kind) || 'online',
    };
    if (Array.isArray(body.model_profiles)) body.model_profiles = [fake];
    if (Array.isArray(body.providers)) body.providers = [fake];
    body.default_model_key = key;
    return route.fulfill({ response, body: JSON.stringify(body) });
  });
  // 新会话里允许首轮 Run 仍在运行：恢复轮询只查一次就绪状态，不串台真实任务事件。
  await page.route('**/api/runs*', (route) => {
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ runs: [] }) });
  });
  // 工作台偏好：POST 拦截到内存对象；GET bootstrap 仍由隔离实例返回（本测试随后注入需要的值）。
  await page.route('**/api/settings', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    const body = JSON.parse(route.request().postData() || '{}');
    settingsPosts.push(body);
    const patch = body.studio;
    if (patch && typeof patch === 'object') {
      if ('last_selection' in patch) studioPrefsState.last_selection = patch.last_selection;
      if (Array.isArray(patch.favorites)) studioPrefsState.favorites = patch.favorites;
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ settings: { studio: studioPrefsState } }),
    });
  });
  // 图片上传：不真落盘；按调用序号返回稳定的 path/thumb_path，供前端预览与任务 payload 断言。
  await page.route('**/api/uploads', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    uploadCounter += 1;
    if (uploadDelayMs) await new Promise((resolve) => setTimeout(resolve, uploadDelayMs));
    if (uploadFailuresLeft > 0) {
      uploadFailuresLeft -= 1;
      routeErrorExemptions.push({ path: '/api/uploads', status: 500 });
      return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: '冒烟注入：上传失败' }) });
    }
    const id = uploadCounter;
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        name: `smoke-${id}.png`,
        path: `C:\\smoke\\uploads\\smoke-${id}.png`,
        thumb_path: `C:\\smoke\\uploads\\smoke-${id}_thumb.webp`,
        size: 68,
      }),
    });
  });
  // 图片反推：ok / partial / fail 三种注入。
  await page.route('**/api/studio/vision', (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    visionCalls.push(body);
    const images = Array.isArray(body.images) ? body.images : [];
    if (visionMode === 'fail') {
      routeErrorExemptions.push({ path: '/api/studio/vision', status: 502 });
      return route.fulfill({
        status: 502, contentType: 'application/json',
        body: JSON.stringify({
          error: '冒烟注入：视觉后端不可用', analyzed: 0, failed: images.length, text: '',
          images: images.map((_, index) => ({ index: index + 1, text: '', error: '视觉后端不可用' })),
        }),
      });
    }
    if (visionMode === 'partial') {
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          ok: false, analyzed: 1, failed: Math.max(1, images.length - 1),
          text: '【图1】\n第一张图的描述',
          images: [
            { index: 1, text: '第一张图的描述', error: '' },
            { index: 2, text: '', error: '第二张识别超时' },
          ],
        }),
      });
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        ok: true, analyzed: images.length, failed: 0,
        text: '【图1】\n第一张图的描述\n\n【图2】\n第二张图的描述',
        images: images.map((_, index) => ({ index: index + 1, text: `第${index + 1}张图的描述`, error: '' })),
      }),
    });
  });
  // 预览图：统一返回 1×1 PNG，避免假路径产生破图/噪声。
  await page.route('**/api/file*', (route) => {
    return route.fulfill({ status: 200, contentType: 'image/png', body: PIXEL_PNG });
  });

  const failures = [];
  const check = (label, ok, detail = '') => {
    console.log(`  [${vp.name}] ${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` -> ${detail}` : ''}`);
    if (!ok) failures.push(label);
  };
  const isMobile = vp.width <= 760;
  // 手机要先进抽屉才点得到侧栏入口；桌面侧栏常驻。
  // 工作台态对话顶栏整体退场，汉堡按钮由工作台模块自己的 #studioOpenSidebar 提供。
  const openDrawerIfMobile = async () => {
    if (!isMobile) return;
    const state = await page.evaluate(() => ({
      open: document.querySelector('#sidebar')?.classList.contains('open'),
      studio: document.body.dataset.studio === 'on',
    }));
    if (state.open) return;
    await page.click(state.studio ? '#studioOpenSidebar' : '#openSidebar');
    await page.waitForTimeout(350);
  };
  const enterStudio = async () => {
    await openDrawerIfMobile();
    await page.click('#openStudio');
    await page.waitForTimeout(1400);
  };
  const exitStudio = async () => {
    await openDrawerIfMobile();
    await page.click('#openChatMode');
    await page.waitForTimeout(500);
  };
  const openHistory = async () => {
    // 「历史产出」入口已从侧栏移到结果栏标题区（侧栏现在是功能页签）：
    // 手机先切「结果」档。已在历史态时入口随操作行隐藏，先「返回生成结果」
    // 退出去再进，保证每次进入都重新拉取列表。
    if (isMobile) await selectPane('result');
    const already = await page.evaluate(() => !document.querySelector('#studioHistoryList').hidden);
    if (already) {
      await page.click('#studioBackToResult');
      await page.waitForTimeout(500);
    }
    await page.click('#studioOpenHistory');
    await page.waitForTimeout(800);
  };
  const selectPane = async (pane) => {
    if (isMobile) {
      await page.click(`[data-studio-pane="${pane}"]`);
      await page.waitForTimeout(220);
    }
  };
  // 退出历史列表并读取标题区最近任务 chips（手机需在「结果」档才看得到）。
  const chipsAfterLeavingHistory = async () => {
    await selectPane('list');
    await page.click('#studioCats [data-studio-module=""]');
    await page.waitForTimeout(600);
    await selectPane('result');
    return page.evaluate(() => document.querySelectorAll('#studioTaskHistory .studio-task-row').length);
  };

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForTimeout(3000);
    // 隔离实例是全新安装：首次启动引导会弹在 top layer 拦截点击，先关掉再继续。
    const onboardingOpen = await page.evaluate(() => document.querySelector('#onboardingDialog')?.open);
    if (onboardingOpen) {
      await page.click('#onboardingDialog [data-close="onboardingDialog"]');
      await page.waitForTimeout(400);
    }

    // 1. 入口唯一且不在顶栏
    const entry = await page.evaluate(() => ({
      count: document.querySelectorAll('#openStudio').length,
      inTopbar: Boolean(document.querySelector('.topbar-actions #openStudio')),
      inSidebar: Boolean(document.querySelector('#sidebar #openStudio')),
      chatMode: Boolean(document.querySelector('#openChatMode')),
      topbarHasStudioText: [...document.querySelectorAll('.topbar-actions .control-button')]
        .some((el) => (el.textContent || '').includes('工作台')),
    }));
    check('工作台入口唯一且在侧栏（顶栏已无）',
      entry.count === 1 && entry.inSidebar && !entry.inTopbar && entry.chatMode && !entry.topbarHasStudioText,
      JSON.stringify(entry));

    // 2. 进入工作台
    await enterStudio();
    const opened = await page.evaluate(() => ({
      body: document.body.dataset.studio,
      viewVisible: !document.querySelector('#studioView').hidden,
      studioSelected: document.querySelector('#openStudio').getAttribute('aria-pressed'),
      chatSelected: document.querySelector('#openChatMode').getAttribute('aria-pressed'),
      navVisible: !document.querySelector('#sidebarStudioNav').hidden,
      treeHidden: getComputedStyle(document.querySelector('#sidebarWorkspaceTree')).display === 'none',
      drawerOpen: document.querySelector('#sidebar').classList.contains('open'),
    }));
    check('工作台视图打开且侧栏选中态同步',
      opened.body === 'on' && opened.viewVisible && opened.studioSelected === 'true'
      && opened.chatSelected === 'false' && opened.navVisible && opened.treeHidden
      && (!isMobile || !opened.drawerOpen),
      JSON.stringify(opened));

    // 2b. 工作台是独立模块：对话外壳（顶栏、输入区）整体退场，模块顶栏占满主区
    const isolation = await page.evaluate(() => {
      const disp = (sel) => { const el = document.querySelector(sel); return el ? getComputedStyle(el).display : 'missing'; };
      const view = document.querySelector('#studioView').getBoundingClientRect();
      const main = document.querySelector('.chat-main').getBoundingClientRect();
      return {
        chatTopbar: disp('.topbar'),
        composer: disp('.composer-wrap'),
        studioTopbar: disp('.studio-topbar'),
        studioTop: Math.round(view.top),
        mainTop: Math.round(main.top),
        hasApiSelect: Boolean(document.querySelector('#studioView #modelSelect, #studioView #agentSelect, #studioView #openSkills')),
        pages: [...document.querySelectorAll('#studioPages .studio-page')].map((el) => el.textContent.trim()),
      };
    });
    check('工作台不继承对话顶栏/输入区（独立模块）',
      isolation.chatTopbar === 'none' && isolation.composer === 'none'
      && isolation.studioTopbar !== 'none' && isolation.studioTop === isolation.mainTop,
      JSON.stringify(isolation));
    check('工作台顶层分页首项为「提词库」（可扩展 tab；3.7.4 起由「模块库」更名，页签可多于一页）',
      isolation.pages.length >= 1 && isolation.pages[0] === '提词库' && !isolation.hasApiSelect,
      JSON.stringify(isolation));

    // 3. 重复点「工作台」不退出
    await openDrawerIfMobile();
    await page.click('#openStudio');
    await page.waitForTimeout(400);
    check('重复点工作台不退出', await page.evaluate(() => document.body.dataset.studio === 'on'));

    // 4. 纵向分类带计数，且不是横向 tab
    const cats = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('#studioCats .studio-cat')];
      return {
        count: rows.length,
        hasCount: rows.some((row) => /\d/.test(row.textContent)),
        vertical: rows.length ? getComputedStyle(document.querySelector('#studioCats')).flexDirection === 'column' : false,
        oldTabs: document.querySelectorAll('.studio-tab').length,
        ariaPressed: rows.every((row) => row.hasAttribute('aria-pressed')),
        tabSemantics: document.querySelectorAll('#studioView [role="tab"], #studioView [role="tablist"]').length,
        searchVisible: Boolean(document.querySelector('#studioSearch')?.offsetWidth),
      };
    });
    check('分类为纵向（≥10 项）、带计数、旧横向 tab 已退役',
      cats.count >= 10 && cats.hasCount && cats.vertical && cats.oldTabs === 0 && cats.searchVisible,
      JSON.stringify(cats));
    check('分档/分类/模式切换不再声明未实现的 tab 语义',
      cats.ariaPressed && cats.tabSemantics === 0, JSON.stringify(cats));

    // 4b. 键盘可达：分类是原生按钮，Enter 即可选中（不需要方向键 tabs 模型）
    // 手机此时停在参数档，分类面板不可见，键盘用例在桌面验证。
    if (!isMobile) {
      await page.focus('#studioCats [data-studio-module="vi"]');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(600);
      check('键盘 Enter 可选中分类', await page.evaluate(() =>
        document.querySelector('#studioCats [data-studio-module="vi"]').getAttribute('aria-pressed') === 'true'));
      await page.focus('#studioCats [data-studio-module=""]');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(600);
    }

    // 5. 列表渲染
    await selectPane('list');
    const rows = await page.locator('.studio-row').count();
    check('模板列表有行', rows > 0, `rows=${rows}`);

    // 5b. 搜索真的过滤列表（不是只保存关键词）
    const firstTitle = (await page.locator('.studio-row .studio-row-title').first().textContent()) || '';
    const needle = firstTitle.trim().slice(0, 2);
    await page.fill('#studioSearch', needle);
    await page.waitForTimeout(700);
    const filtered = await page.evaluate((term) => {
      const titles = [...document.querySelectorAll('#studioList .studio-row-title')].map((el) => el.textContent);
      return {
        count: titles.length,
        allMatch: titles.every((text) => text.toLowerCase().includes(term.toLowerCase())),
      };
    }, needle);
    check('搜索词实际过滤模板列表', needle.length > 0 && filtered.count > 0 && filtered.allMatch
      && filtered.count <= rows, JSON.stringify({ needle, ...filtered, before: rows }));
    await page.fill('#studioSearch', 'zzzz-冒烟无匹配');
    await page.waitForTimeout(700);
    check('无匹配搜索显示空态', await page.evaluate(() => {
      const text = document.querySelector('#studioList').textContent || '';
      return text.includes('没有匹配条目');
    }));
    await page.fill('#studioSearch', '');
    await page.waitForTimeout(700);

    // 6. 选第一个条目 → 详情/预览出现（手机需先切模板档）
    await selectPane('list');
    const firstRow = page.locator('.studio-row').first();
    const rowTitle = (await firstRow.locator('.studio-row-title').textContent()) || '';
    await firstRow.click();
    await page.waitForTimeout(900);
    const detail = await page.evaluate(() => ({
      title: document.querySelector('#studioDetailTitle').textContent,
      body: document.querySelector('#studioBodyPreview').textContent.length,
      chips: document.querySelectorAll('#studioDetailChips .studio-chip').length,
    }));
    check('详情标题=所选行', detail.title === rowTitle, `${detail.title} vs ${rowTitle}`);
    check('正文预览非空', detail.body > 0, `len=${detail.body}`);

    // 7. 手机：选模板后自动切参数档；桌面始终三栏
    if (isMobile) {
      const pane = await page.evaluate(() => ({
        pane: document.querySelector('#studioView').dataset.pane,
        formVisible: getComputedStyle(document.querySelector('#studioDetail')).display !== 'none',
        resultHidden: getComputedStyle(document.querySelector('.studio-result')).display === 'none',
      }));
      check('手机选模板后切参数档（另两档不占布局）',
        pane.pane === 'form' && pane.formVisible && pane.resultHidden, JSON.stringify(pane));
    } else {
      const cols = await page.evaluate(() => {
        const vis = (sel) => getComputedStyle(document.querySelector(sel)).display !== 'none';
        return { library: vis('.studio-library'), detail: vis('#studioDetail'), result: vis('.studio-result') };
      });
      check('桌面始终三栏可见', cols.library && cols.detail && cols.result, JSON.stringify(cols));
    }

    // 8. 一级输入位存在（用户视角的最终输入）
    check('「我的输入」可见', await page.locator('#studioUserRequest').isVisible());

    // 9. 标题区最近任务最多 3 条（接口注入了 6 条），且有图标/状态/相对时间
    const recent = await page.evaluate(() => {
      const chips = [...document.querySelectorAll('#studioTaskHistory .studio-task-row')];
      return {
        count: chips.length,
        chips: chips.map((el) => ({
          name: el.querySelector('.studio-task-text b')?.textContent.trim() || '',
          icon: Boolean(el.querySelector('.studio-task-icon')),
          status: el.querySelector('.studio-task-text em')?.textContent.trim() || '',
          time: el.querySelector('.studio-task-text small span:last-child')?.textContent.trim() || '',
          chevron: Boolean(el.querySelector('.studio-task-open')),
        })),
      };
    });
    check('最近任务 chips 不超过 3 条', recent.count > 0 && recent.count <= 3, JSON.stringify(recent));
    check('最近任务有状态图标、状态文字、相对时间和可点提示',
      recent.chips.every((chip) => chip.name && chip.icon && chip.status && chip.time && chip.chevron),
      JSON.stringify(recent.chips));

    // 10. 侧栏「历史产出」→ 完整列表（6 条），标题区捷径隐藏
    await openHistory();
    const history = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('#studioHistoryList .studio-history-row')];
      const metaById = {};
      rows.forEach((row) => { metaById[row.dataset.studioTask] = row.querySelector('.studio-history-meta').textContent; });
      return {
        visible: !document.querySelector('#studioHistoryList').hidden,
        count: rows.length,
        titles: rows.map((row) => row.querySelector('.studio-history-title').textContent),
        metaById,
        chipsHidden: document.querySelector('#studioTaskHistory').hidden,
        streamHidden: document.querySelector('#studioResultStream').hidden,
        title: document.querySelector('#studioResultTitle').textContent,
        range: document.querySelector('#studioHistoryRange').textContent,
      };
    });
    check('历史产出显示完整列表（接口条数一致）',
      history.visible && history.count === taskList.length, JSON.stringify({ count: history.count, want: taskList.length }));
    check('历史态隐藏最近任务 chips 与结果流',
      history.chipsHidden && history.streamHidden, JSON.stringify(history));
    check('历史范围提示不承诺「全部历史」',
      history.range.includes('60') && !history.range.includes('全部'), history.range);
    check('缺标题/时间/来源有回退文案',
      history.titles.includes('未命名任务')
      && history.metaById['st-smoke-0004'].includes('时间未记录')
      && history.metaById['st-smoke-0005'].includes('来源未记录')
      && history.metaById['st-smoke-0002'].includes('文生图'),
      JSON.stringify({ titles: history.titles, meta0004: history.metaById['st-smoke-0004'], meta0005: history.metaById['st-smoke-0005'] }));
    check('状态中文化（不露英文码）',
      !Object.values(history.metaById).some((text) => /queued|running|failed|interrupted|cancelled|done/.test(text)),
      JSON.stringify(history.metaById));

    // 10b. 历史产出态必须有「返回生成结果」按钮，不能进了历史就出不来
    const backButton = await page.evaluate(() => ({
      visible: !document.querySelector('#studioBackToResult').hidden,
      label: document.querySelector('#studioBackToResult').textContent.trim(),
    }));
    check('历史产出有「返回生成结果」按钮', backButton.visible && backButton.label.includes('返回'), JSON.stringify(backButton));
    await page.click('#studioBackToResult');
    await page.waitForTimeout(500);
    const backToResult = await page.evaluate(() => ({
      historyHidden: document.querySelector('#studioHistoryList').hidden,
      streamVisible: !document.querySelector('#studioResultStream').hidden,
      backHidden: document.querySelector('#studioBackToResult').hidden,
    }));
    check('点「返回生成结果」回到结果态',
      backToResult.historyHidden && backToResult.streamVisible && backToResult.backHidden,
      JSON.stringify(backToResult));
    await openHistory();

    // 11. 点历史行 → 回到结果态并显示结果；「继续追问」可用
    await page.click(`#studioHistoryList [data-studio-task="${TASK_ID}"]`);
    await page.waitForTimeout(700);
    const picked = await page.evaluate(() => ({
      mode: document.querySelector('#studioHistoryList').hidden,
      streamVisible: !document.querySelector('#studioResultStream').hidden,
      handoff: !document.querySelector('#studioHandoff').hidden,
      chipCount: document.querySelectorAll('#studioTaskHistory .studio-task-row').length,
    }));
    check('点历史行显示结果并退出列表模式', picked.mode && picked.streamVisible, JSON.stringify(picked));
    check('完成任务后「继续追问」可用', picked.handoff, JSON.stringify(picked));

    // 12. 手机：历史行 → 结果档，任务动作在结果档可访问
    if (isMobile) {
      const pane = await page.evaluate(() => ({
        pane: document.querySelector('#studioView').dataset.pane,
        actionsVisible: getComputedStyle(document.querySelector('.studio-result-actions')).display !== 'none',
      }));
      check('手机点历史行切结果档且任务动作可达', pane.pane === 'result' && pane.actionsVisible, JSON.stringify(pane));
    }

    // 13. 追问框：初始无选中，「创建会话并继续」禁用；取消只关闭不触达交接接口
    await page.click('#studioHandoff');
    await page.waitForTimeout(700);
    const dialogState = await page.evaluate(() => {
      const dialog = document.querySelector('#studioAgentDialog');
      if (!dialog || !dialog.open) return { open: false };
      const rows = dialog.querySelectorAll('.studio-agent-row');
      return {
        open: true,
        rows: rows.length,
        pickedActive: dialog.querySelectorAll('.studio-agent-row.active').length,
        confirmDisabled: document.querySelector('#studioHandoffConfirm').disabled,
      };
    });
    check('追问框已打开且有 Agent 列表', dialogState.open && dialogState.rows > 0, JSON.stringify(dialogState));
    check('初始无选中 Agent 且禁用继续', dialogState.pickedActive === 0 && dialogState.confirmDisabled, JSON.stringify(dialogState));
    await page.click('#studioAgentDialog [data-close="studioAgentDialog"]');
    await page.waitForTimeout(400);
    check('取消关闭追问框', await page.evaluate(() => !document.querySelector('#studioAgentDialog').open));
    check('取消未触达交接接口', handoffCalls.length === 0, JSON.stringify(handoffCalls));

    // 14. 重新打开 → 显式选中一个 Agent → 「继续」可用
    await page.click('#studioHandoff');
    await page.waitForTimeout(500);
    await page.click('.studio-agent-row');
    await page.waitForTimeout(200);
    check('显式选中后「继续」可用', await page.evaluate(() => !document.querySelector('#studioHandoffConfirm').disabled));
    await page.click('#studioAgentDialog [data-close="studioAgentDialog"]');
    await page.waitForTimeout(300);
    check('选中后取消也不创建会话', handoffCalls.length === 0, JSON.stringify(handoffCalls));

    // 15. 历史读取失败 → 明确失败与重试入口；重试后恢复
    historyFailuresLeft = 1;
    await openHistory();
    const failed = await page.evaluate(() => ({
      errorShown: Boolean(document.querySelector('#studioHistoryList .studio-history-error')),
      retry: Boolean(document.querySelector('#studioHistoryList [data-studio-history-retry]')),
      notice: Boolean(document.querySelector('#studioHistoryList .studio-history-notice')),
    }));
    check('历史读取失败显示失败与重试入口', failed.errorShown && failed.retry && !failed.notice, JSON.stringify(failed));
    await page.click('#studioHistoryList [data-studio-history-retry]');
    await page.waitForTimeout(900);
    const recovered = await page.evaluate(() => ({
      errorShown: Boolean(document.querySelector('#studioHistoryList .studio-history-error')),
      rows: document.querySelectorAll('#studioHistoryList .studio-history-row').length,
    }));
    check('重试后历史列表恢复', !recovered.errorShown && recovered.rows === taskList.length, JSON.stringify(recovered));

    // 16. 空历史
    taskList = [];
    await openHistory();
    const emptyState = await page.evaluate(() => ({
      text: document.querySelector('#studioHistoryList').textContent,
      rows: document.querySelectorAll('#studioHistoryList .studio-history-row').length,
    }));
    check('空历史显示「还没有历史产出」', emptyState.rows === 0 && emptyState.text.includes('还没有历史产出'), JSON.stringify(emptyState));

    // 17. 选分类退出历史模式（历史列表不再显示）
    taskList = freshTaskList();
    await openHistory();
    await selectPane('list');
    await page.click('#studioCats [data-studio-module="vs"]');
    await page.waitForTimeout(700);
    const afterCat = await page.evaluate(() => ({
      historyHidden: document.querySelector('#studioHistoryList').hidden,
      selected: document.querySelector('#studioCats [data-studio-module="vs"]').getAttribute('aria-pressed'),
      streamVisible: !document.querySelector('#studioResultStream').hidden,
    }));
    check('点分类退出历史列表并选中分类',
      afterCat.historyHidden && afterCat.selected === 'true' && afterCat.streamVisible,
      JSON.stringify(afterCat));

    // 17b. 终态任务显示保留的已产出部分；切到无正文任务时清掉上一条的正文
    await openHistory();
    await page.click('#studioHistoryList [data-studio-task="st-smoke-0004"]');
    await page.waitForTimeout(600);
    const partialState = await page.evaluate(() => ({
      stream: document.querySelector('#studioResultStream').textContent,
      hint: document.querySelector('#studioTaskHint').textContent,
    }));
    check('中断任务显示已产出的部分正文',
      partialState.stream.includes('已生成的部分') && partialState.hint.includes('已中断'),
      JSON.stringify(partialState));
    await openHistory();
    await page.click('#studioHistoryList [data-studio-task="st-smoke-0005"]');
    await page.waitForTimeout(600);
    const clearedState = await page.evaluate(() => ({
      stream: document.querySelector('#studioResultStream').textContent,
      hint: document.querySelector('#studioTaskHint').textContent,
    }));
    check('切到无正文任务时不残留上一条结果',
      clearedState.stream === '' && clearedState.hint.includes('已取消'),
      JSON.stringify(clearedState));

    // 18. 手机：默认参数档；三档切换互斥
    if (isMobile) {
      await page.click('[data-studio-pane="form"]');
      await page.waitForTimeout(200);
      const panes = await page.evaluate(() => {
        const vis = (sel) => getComputedStyle(document.querySelector(sel)).display !== 'none';
        return {
          library: vis('.studio-library'),
          detail: vis('#studioDetail'),
          result: vis('.studio-result'),
          pane: document.querySelector('#studioView').dataset.pane,
        };
      });
      check('手机参数档只显示参数面板', panes.pane === 'form' && panes.detail && !panes.library && !panes.result, JSON.stringify(panes));
      // 切档只改显示，不清空草稿。
      await page.fill('#studioUserRequest', '切档草稿保留测试');
      await page.click('[data-studio-pane="list"]');
      await page.waitForTimeout(160);
      await page.click('[data-studio-pane="form"]');
      await page.waitForTimeout(160);
      check('手机切档保留「我的输入」草稿',
        (await page.inputValue('#studioUserRequest')) === '切档草稿保留测试');
      // 档位按钮是原生按钮：键盘 Enter 即可切换。
      await page.focus('[data-studio-pane="list"]');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(220);
      check('键盘 Enter 可切换手机档位', await page.evaluate(() =>
        document.querySelector('[data-studio-pane="list"]').getAttribute('aria-pressed') === 'true'
        && document.querySelector('#studioView').dataset.pane === 'list'));
      await page.focus('[data-studio-pane="form"]');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(220);
    }

    // 19. 侧栏工作台导航是三个功能页签（3.7.x 起侧栏承担分页）；「历史产出」
    //     入口移到结果栏标题区，是带 title 的图标按钮。
    if (isMobile) await selectPane('result');
    const nav = await page.evaluate(() => {
      const tabs = [...document.querySelectorAll('#studioPages .studio-page')].map((btn) => ({
        text: btn.textContent.trim(),
        current: btn.getAttribute('aria-current') === 'page',
      }));
      const historyBtn = document.querySelector('#studioOpenHistory');
      return {
        tabs,
        historyTitle: historyBtn?.getAttribute('title') || '',
        historyVisible: Boolean(historyBtn && historyBtn.getBoundingClientRect().width > 0),
      };
    });
    check('侧栏工作台导航是 提词库/打标器/造梦机 三个页签',
      nav.tabs.length === 3
      && nav.tabs.map((t) => t.text).join('/') === '提词库/打标器/造梦机',
      JSON.stringify(nav));
    check('「历史产出」是结果栏标题区的图标按钮',
      nav.historyTitle === '历史产出' && nav.historyVisible, JSON.stringify(nav));

    // 20. 轮询不覆盖历史列表；离开工作台停止轮询但不取消后端任务
    // 事件接口持续返回 running，才能真正验证「轮询在跑 + 离开后停」。
    eventStatus = 'running';
    await openHistory();
    const listBefore = await page.evaluate(() => document.querySelectorAll('#studioHistoryList .studio-history-row').length);
    // 选一条运行中任务：既启动轮询，又让结果态处于生成中。
    await page.evaluate(() => {
      const rows = [...document.querySelectorAll('#studioHistoryList .studio-history-row')];
      const target = rows.find((row) => row.textContent.includes('生成中')) || rows[0];
      target?.click();
    });
    await page.waitForTimeout(300);
    const pollingActive = eventRequests;
    // 取消动作在结果档可达，并遵循真实契约 { ok: true }。
    await selectPane('result');
    await page.click('#studioCancelTask');
    await page.waitForTimeout(300);
    check('取消任务走真实接口契约', cancelCalls === 1, `cancelCalls=${cancelCalls}`);
    await openHistory();
    await page.waitForTimeout(2600);
    const stillHistory = await page.evaluate(() => ({
      historyVisible: !document.querySelector('#studioHistoryList').hidden,
      streamHidden: document.querySelector('#studioResultStream').hidden,
      rows: document.querySelectorAll('#studioHistoryList .studio-history-row').length,
      title: document.querySelector('#studioResultTitle').textContent,
    }));
    check('轮询期间历史列表不被抢走',
      stillHistory.historyVisible && stillHistory.streamHidden
      && stillHistory.rows === listBefore && stillHistory.title === '历史产出',
      JSON.stringify({ ...stillHistory, listBefore }));
    check('选中运行中任务确实在轮询', eventRequests > pollingActive,
      `atPick=${pollingActive} now=${eventRequests}`);
    await exitStudio();
    await page.waitForTimeout(400);
    const requestsAtExit = eventRequests;
    await page.waitForTimeout(2600);
    check('离开工作台停止前端轮询', eventRequests === requestsAtExit,
      `atExit=${requestsAtExit} after=${eventRequests}`);
    check('离开工作台不取消后端任务', cancelCalls === 1, `cancelCalls=${cancelCalls}`);

    // 21. 离开期间任务终结：重进按服务端状态恢复，不把已结束任务当运行中
    const runningTask = taskList.find((row) => row.id === 'st-smoke-0002');
    Object.assign(runningTask, { status: 'done', result: '离开期间已完成的结果正文。', result_partial: '' });
    eventStatus = 'done';
    await page.waitForTimeout(200);
    const requestsBeforeTerminalEnter = eventRequests;
    await enterStudio();
    const terminalReenter = await page.evaluate(() => ({
      stream: document.querySelector('#studioResultStream').textContent,
      cancelHidden: document.querySelector('#studioCancelTask').hidden,
      retryHidden: document.querySelector('#studioRetryTask').hidden,
      handoffHidden: document.querySelector('#studioHandoff').hidden,
    }));
    check('离开期间完成的终端状态在重进后生效',
      terminalReenter.stream.includes('离开期间已完成的结果正文')
      && terminalReenter.cancelHidden && terminalReenter.retryHidden && !terminalReenter.handoffHidden,
      JSON.stringify(terminalReenter));
    await page.waitForTimeout(2600);
    check('重进后不给已终结任务继续轮询', eventRequests === requestsBeforeTerminalEnter,
      `before=${requestsBeforeTerminalEnter} after=${eventRequests}`);

    // 22. 仍运行的任务重进后恢复订阅（同时验证历史入口可用）
    Object.assign(runningTask, { status: 'running', result: '', result_partial: '仍在产出的正文，' });
    eventStatus = 'running';
    await openHistory();
    await page.click('#studioHistoryList [data-studio-task="st-smoke-0002"]');
    await page.waitForTimeout(400);
    await exitStudio();
    const requestsBeforeRunningEnter = eventRequests;
    await enterStudio();
    await page.waitForTimeout(1600);
    check('重新进入后按服务端状态恢复运行中任务订阅', eventRequests > requestsBeforeRunningEnter,
      `before=${requestsBeforeRunningEnter} after=${eventRequests}`);

    // 23. 快速切换任务：A 的迟到响应不得覆盖新建的 B
    // 先准备输入，再点开慢响应的历史任务 A，紧接着生成任务 B。
    // 生成任务本身是完成态，事件流也切成 done，避免上一段运行中任务继续拖轮询。
    eventStatus = 'done';
    await selectPane('form');
    await page.fill('#studioUserRequest', '冒烟生成请求：画一只猫');
    await openHistory();
    slowDetailMs.set('st-smoke-0006', 1500);
    await page.click('#studioHistoryList [data-studio-task="st-smoke-0006"]');
    await page.waitForTimeout(120);
    await selectPane('form');
    await page.click('#studioGenerate');
    await page.waitForTimeout(2400);
    const lateGuard = await page.evaluate(() => ({
      stream: document.querySelector('#studioResultStream').textContent,
      historyHidden: document.querySelector('#studioHistoryList').hidden,
      pane: document.querySelector('#studioView').dataset.pane,
    }));
    check('任务详情 A 迟到时新建 B 不被覆盖',
      lateGuard.stream.includes('生成结果正文')
      && !lateGuard.stream.includes('第二条结果正文'),
      JSON.stringify({ stream: lateGuard.stream.slice(0, 40), mode: lateGuard.historyHidden ? 'result' : 'history' }));
    slowDetailMs.delete('st-smoke-0006');
    check('生成请求带上用户输入（自由输入 → 生成）',
      generateCalls.length === 1 && String(generateCalls[0].user_request || '').includes('画一只猫'),
      JSON.stringify(generateCalls[0] || {}));

    // 24. 自定义条目迟到加载不覆盖新选择（桌面：不用处理三档，竞态本身与视口无关）
    if (!isMobile) {
      await page.click('#studioCats [data-studio-module="custom"]');
      await page.waitForTimeout(600);
      await page.click('.studio-row[data-studio-kind="custom"]');
      await page.waitForTimeout(500);
      // 保存会让自定义列表缓存失效，随后慢速重读；在它返回前再次点条目并切到内置条目。
      customDelayMs = 1400;
      await page.click('#studioCustomSave');
      await page.waitForTimeout(120);
      await page.click('.studio-row[data-studio-kind="custom"]');
      await page.waitForTimeout(120);
      await page.click('#studioCats [data-studio-module="vs"]');
      await page.waitForTimeout(500);
      const builtinTitle = (await page.locator('.studio-row .studio-row-title').first().textContent()) || '';
      await page.click('.studio-row[data-studio-kind="builtin"]');
      await page.waitForTimeout(2600);
      customDelayMs = 0;
      const customLate = await page.evaluate(() => ({
        title: document.querySelector('#studioDetailTitle').textContent,
        customButtonsHidden: document.querySelector('#studioCustomSave').hidden,
      }));
      check('自定义条目迟到加载不覆盖新选中的内置条目',
        customLate.title === builtinTitle && customLate.customButtonsHidden,
        JSON.stringify({ ...customLate, builtinTitle }));
    }

    // 25. 历史列表数量边界：0 / 1 / 3 / 60；chips = min(总数, 3)
    for (const count of [0, 1, 3, 60]) {
      taskList = boundaryTasks(count);
      await openHistory();
      const historyRows = await page.evaluate(() => document.querySelectorAll('#studioHistoryList .studio-history-row').length);
      check(`历史列表边界 ${count} 条`, historyRows === count, `rows=${historyRows}`);
      const chips = await chipsAfterLeavingHistory();
      check(`最近任务 chips 边界 ${count} 条 = min(总数,3)`, chips === Math.min(count, 3), `chips=${chips}`);
    }
    taskList = freshTaskList();

    // 26. 桌面：侧栏收起后仍有恢复入口（工作台态对话顶栏已退场）
    if (!isMobile) {
      await openDrawerIfMobile();
      await page.click('#collapseSidebar');
      await page.waitForTimeout(400);
      const collapsed = await page.evaluate(() => ({
        shell: document.querySelector('#appShell').classList.contains('sidebar-collapsed'),
        expandVisible: Boolean(document.querySelector('#studioExpandSidebar')?.offsetWidth || document.querySelector('#studioExpandSidebar')?.offsetHeight),
        chatModeVisible: Boolean(document.querySelector('#openChatMode')?.offsetWidth),
      }));
      check('工作台态收起侧栏后仍有可见的展开入口',
        collapsed.shell && collapsed.expandVisible, JSON.stringify(collapsed));
      await page.click('#studioExpandSidebar');
      await page.waitForTimeout(400);
      const expanded = await page.evaluate(() => ({
        shell: document.querySelector('#appShell').classList.contains('sidebar-collapsed'),
        chatModeVisible: Boolean(document.querySelector('#openChatMode')?.offsetWidth),
      }));
      check('展开后回到侧栏并能选「对话」',
        !expanded.shell && expanded.chatModeVisible, JSON.stringify(expanded));

      // 27. 跨断点（1280 → 760 → 761 → 1280）不清空页面状态
      await page.fill('#studioUserRequest', '断点保留测试');
      await page.setViewportSize({ width: 760, height: 812 });
      await page.waitForTimeout(500);
      const at760 = await page.evaluate(() => ({
        pane: document.querySelector('#studioView').dataset.pane,
        input: document.querySelector('#studioUserRequest').value,
        panesVisible: getComputedStyle(document.querySelector('#studioPanes')).display !== 'none',
        onePane: ['.studio-library', '#studioDetail', '.studio-result']
          .filter((sel) => getComputedStyle(document.querySelector(sel)).display !== 'none').length,
      }));
      check('760px 切到手机三档且草稿保留',
        at760.panesVisible && at760.onePane === 1 && at760.input === '断点保留测试', JSON.stringify(at760));
      await page.setViewportSize({ width: 761, height: 812 });
      await page.waitForTimeout(500);
      const at761 = await page.evaluate(() => ({
        input: document.querySelector('#studioUserRequest').value,
        columns: ['.studio-library', '#studioDetail', '.studio-result']
          .filter((sel) => getComputedStyle(document.querySelector(sel)).display !== 'none').length,
        panesVisible: getComputedStyle(document.querySelector('#studioPanes')).display !== 'none',
      }));
      check('761px 恢复桌面三栏且草稿保留',
        at761.columns === 3 && !at761.panesVisible && at761.input === '断点保留测试', JSON.stringify(at761));
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.waitForTimeout(500);
      check('回到 1280px 仍是桌面三栏',
        await page.evaluate(() => ['.studio-library', '#studioDetail', '.studio-result']
          .filter((sel) => getComputedStyle(document.querySelector(sel)).display !== 'none').length === 3));
    }

    // 28. 返回对话视图
    await exitStudio();
    const back = await page.evaluate(() => ({
      body: document.body.dataset.studio,
      viewHidden: document.querySelector('#studioView').hidden,
      navHidden: document.querySelector('#sidebarStudioNav').hidden,
      treeVisible: getComputedStyle(document.querySelector('#sidebarWorkspaceTree')).display !== 'none',
      chatSelected: document.querySelector('#openChatMode').getAttribute('aria-pressed'),
    }));
    check('点「对话」返回对话区并恢复侧栏会话树',
      back.body === 'off' && back.viewHidden && back.navHidden && back.treeVisible && back.chatSelected === 'true',
      JSON.stringify(back));

    // 29. 键盘可完成「对话 → 工作台 → 对话」切换（桌面侧栏常驻）
    if (!isMobile) {
      await page.focus('#openStudio');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(900);
      const keyboardOn = await page.evaluate(() => document.body.dataset.studio === 'on');
      await page.focus('#openChatMode');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(500);
      const keyboardOff = await page.evaluate(() => document.body.dataset.studio === 'off');
      check('键盘 Enter 可切换对话/工作台', keyboardOn && keyboardOff,
        JSON.stringify({ keyboardOn, keyboardOff }));
    }

    // 30. 手工交接闭环（修复回归守门）：真点「创建会话并继续」。
    // 失败路径一律留在工作台、不重发交接；成功路径恰一次请求并真打开注入的普通会话，
    // 不等首轮 Run 完成（打开会话的阻断点只是会话详情读取）。
    await enterStudio();
    await openHistory();
    await page.click(`#studioHistoryList [data-studio-task="${TASK_ID}"]`);
    await page.waitForTimeout(600);

    // 30a 失败：接口 4xx —— 弹窗与工作台保留、按钮恢复、不跳转、不装作成功
    handoffBehavior = 'http-error';
    await page.click('#studioHandoff');
    await page.waitForTimeout(500);
    await page.click('.studio-agent-row');
    await page.fill('#studioHandoffNote', '失败路径备注');
    let callsBefore = handoffCalls.length;
    await page.click('#studioHandoffConfirm');
    await page.waitForTimeout(600);
    const httpErrorState = await page.evaluate(() => ({
      dialogOpen: document.querySelector('#studioAgentDialog').open,
      studioOn: document.body.dataset.studio === 'on',
      confirmDisabled: document.querySelector('#studioHandoffConfirm').disabled,
      confirmText: document.querySelector('#studioHandoffConfirm').textContent,
      toast: document.querySelector('#toast')?.textContent || '',
    }));
    check('交接 4xx：恰一次请求且请求体正确',
      handoffCalls.length - callsBefore === 1
      && handoffCalls.at(-1).task_id === TASK_ID
      && handoffCalls.at(-1).agent_id === 'agent-smoke-a'
      && handoffCalls.at(-1).note === '失败路径备注',
      JSON.stringify(handoffCalls.at(-1)));
    check('交接 4xx：弹窗与工作台保留、按钮恢复、报错误而非伪装成功',
      httpErrorState.dialogOpen && httpErrorState.studioOn
      && !httpErrorState.confirmDisabled && httpErrorState.confirmText === '创建会话并继续'
      && httpErrorState.toast.includes('交接失败'),
      JSON.stringify(httpErrorState));

    // 30b 失败：响应缺 conversation_id —— 不调用会话详情端点、不退出到空白对话
    handoffBehavior = 'missing-id';
    callsBefore = handoffCalls.length;
    let getsBefore = conversationGetCalls.length;
    await page.click('#studioHandoffConfirm');
    await page.waitForTimeout(600);
    const missingIdState = await page.evaluate(() => ({
      dialogOpen: document.querySelector('#studioAgentDialog').open,
      studioOn: document.body.dataset.studio === 'on',
      toast: document.querySelector('#toast')?.textContent || '',
    }));
    check('响应缺会话 id：请求发出但绝不读会话详情',
      handoffCalls.length - callsBefore === 1 && conversationGetCalls.length - getsBefore === 0,
      `calls=${handoffCalls.length} gets=${conversationGetCalls.length}`);
    check('响应缺会话 id：留在工作台并提示',
      missingIdState.dialogOpen && missingIdState.studioOn && missingIdState.toast.includes('没有会话标识'),
      JSON.stringify(missingIdState));

    // 30c 失败：交接成功但会话详情读取失败 —— 不重发交接、把失败讲清楚
    handoffBehavior = 'ok-open-fail';
    callsBefore = handoffCalls.length;
    let failsBefore = conversationGetFails;
    await page.click('#studioHandoffConfirm');
    await page.waitForTimeout(1200);
    const openFailState = await page.evaluate(async () => {
      const mod = await import('/js/01-core.js');
      return {
        dialogOpen: document.querySelector('#studioAgentDialog').open,
        studioOn: document.body.dataset.studio === 'on',
        toast: document.querySelector('#toast')?.textContent || '',
        convId: mod.state.conversationId,
      };
    });
    check('会话打开失败：恰一次交接请求（不重发）+ 一次会话详情尝试',
      handoffCalls.length - callsBefore === 1 && conversationGetFails - failsBefore === 1,
      `calls=${handoffCalls.length} fails=${conversationGetFails}`);
    check('会话打开失败：进入对话壳、不把失败说成已打开',
      !openFailState.dialogOpen && !openFailState.studioOn
      && openFailState.toast.includes('可能已创建') && openFailState.toast.includes('没能打开'),
      JSON.stringify(openFailState));
    check('会话打开失败：不把失败的 id 设为当前会话',
      openFailState.convId !== OPENFAIL_CONV_ID, String(openFailState.convId));

    // 30d 成功：真点确认 → 忙碌期防护 + 完成前关弹窗不加请求 → 打开注入的普通会话。
    // 注入 600ms 延迟，让忙碌窗口可观察、重复触发与关弹窗可以插进去。
    handoffBehavior = 'ok';
    handoffDelayMs = 600;
    await enterStudio();
    await openHistory();
    await page.click(`#studioHistoryList [data-studio-task="${TASK_ID}"]`);
    await page.waitForTimeout(600);
    await page.click('#studioHandoff');
    await page.waitForTimeout(500);
    await page.click('.studio-agent-row');
    await page.fill('#studioHandoffNote', '冒烟交接成功');
    callsBefore = handoffCalls.length;
    getsBefore = conversationGetCalls.length;
    await page.click('#studioHandoffConfirm');
    await page.waitForTimeout(150);
    const busyState = await page.evaluate(() => ({
      disabled: document.querySelector('#studioHandoffConfirm').disabled,
      text: document.querySelector('#studioHandoffConfirm').textContent,
    }));
    check('交接进行中：按钮进入忙碌/禁用态', busyState.disabled && busyState.text === '正在创建会话…',
      JSON.stringify(busyState));
    await page.dispatchEvent('#studioHandoffConfirm', 'click'); // 忙碌期的程序化重复触发
    await page.click('#studioAgentDialog [data-close="studioAgentDialog"]'); // 完成前关弹窗
    await page.waitForTimeout(2400);
    const handoffSuccess = await page.evaluate(async () => {
      const mod = await import('/js/01-core.js');
      return {
        dialogOpen: document.querySelector('#studioAgentDialog').open,
        studioOn: document.body.dataset.studio === 'on',
        chatSelected: document.querySelector('#openChatMode').getAttribute('aria-pressed'),
        convId: mod.state.conversationId,
        messageText: document.querySelector('#messages')?.textContent || '',
        toast: document.querySelector('#toast')?.textContent || '',
      };
    });
    check('交接成功：恰一次请求（忙碌期重复触发与关弹窗都不再加）',
      handoffCalls.length - callsBefore === 1, `calls=${handoffCalls.length}`);
    check('交接成功：请求体 task_id/agent_id/note 正确',
      handoffCalls.at(-1).task_id === TASK_ID && handoffCalls.at(-1).agent_id === 'agent-smoke-a'
      && handoffCalls.at(-1).note === '冒烟交接成功',
      JSON.stringify(handoffCalls.at(-1)));
    check('交接成功：返回的会话详情被读取（openConversation 完成）',
      conversationGetCalls.length - getsBefore >= 1,
      // 背景会话同步（bootstrap 每 1.8s 轮询当前会话）可能再读一次，属正常行为不计数。
      `gets=${conversationGetCalls.length}`);
    check('交接成功：Agent 弹窗关闭、退出工作台、进入对话模式',
      !handoffSuccess.dialogOpen && !handoffSuccess.studioOn && handoffSuccess.chatSelected === 'true',
      JSON.stringify({ dialogOpen: handoffSuccess.dialogOpen, studioOn: handoffSuccess.studioOn, chatSelected: handoffSuccess.chatSelected }));
    check('交接成功：当前会话就是返回的会话',
      handoffSuccess.convId === HANDOFF_CONV_ID, String(handoffSuccess.convId));
    check('交接成功：消息区显示该会话内容（不等 Run 完成）',
      handoffSuccess.messageText.includes('【来自 Cat Chat 工作台的交接】'),
      handoffSuccess.messageText.slice(0, 60));
    check('交接成功：有完成提示', handoffSuccess.toast.includes('已创建普通会话'), handoffSuccess.toast);
    handoffBehavior = 'forbid';
    handoffDelayMs = 0;

    // ============================================================
    // 31. 记住上次选中的模板（重载后恢复；失效 ID 回安全空态）
    // ============================================================
    await enterStudio();
    await selectPane('list');
    await page.click('#studioCats [data-studio-module="vs"]');
    await page.waitForTimeout(700);
    const restoreRow = page.locator('#studioList .studio-row').first();
    const restoreTitle = (await restoreRow.locator('.studio-row-title').textContent()) || '';
    const restoreId = await restoreRow.getAttribute('data-studio-pick');
    await restoreRow.click();
    await page.waitForTimeout(900);
    await page.waitForTimeout(400); // 等偏好 POST 落定
    check('选择条目会写入工作台偏好（来源+ID+分类）',
      settingsPosts.some((post) => post.studio?.last_selection?.kind === 'builtin'
        && post.studio.last_selection.id === restoreId
        && post.studio.last_selection.module === 'vs'),
      JSON.stringify(settingsPosts.filter((post) => post.studio).slice(-3)));

    await page.reload({ waitUntil: 'load', timeout: 20000 });
    await page.waitForTimeout(2600);
    const onboardingAgain = await page.evaluate(() => document.querySelector('#onboardingDialog')?.open);
    if (onboardingAgain) {
      await page.click('#onboardingDialog [data-close="onboardingDialog"]');
      await page.waitForTimeout(300);
    }
    // 隔离实例的 config 不被本测试污染：把测试态偏好注入 bootstrap。
    await page.evaluate(async (prefs) => {
      const mod = await import('/js/01-core.js');
      mod.state.bootstrap.settings.studio = prefs;
    }, studioPrefsState);
    await enterStudio();
    await page.waitForTimeout(1800);
    const restoredState = await page.evaluate(() => ({
      title: document.querySelector('#studioDetailTitle').textContent,
      module: document.querySelector('#studioCats [aria-pressed="true"]')?.dataset.studioModule,
    }));
    check('重载后恢复上次选中的模板与分类',
      restoredState.title === restoreTitle && restoredState.module === 'vs',
      JSON.stringify({ ...restoredState, restoreTitle }));

    // 失效 ID：重载出新页面，避免沿用内存里已选中的条目（分类切换会保留选择）。
    await page.reload({ waitUntil: 'load', timeout: 20000 });
    await page.waitForTimeout(2600);
    const onboardingInvalid = await page.evaluate(() => document.querySelector('#onboardingDialog')?.open);
    if (onboardingInvalid) {
      await page.click('#onboardingDialog [data-close="onboardingDialog"]');
      await page.waitForTimeout(300);
    }
    await page.evaluate(async () => {
      const mod = await import('/js/01-core.js');
      mod.state.bootstrap.settings.studio = {
        last_selection: { kind: 'builtin', id: 'no-such-entry', module: 'vs', page: 'library' },
        favorites: [],
      };
    });
    // 预期内的一次 404：目录里确实没有这个条目。
    routeErrorExemptions.push({ path: '/api/studio/catalog/no-such-entry', status: 404 });
    await enterStudio();
    await page.waitForTimeout(1400);
    const invalidRestore = await page.evaluate(() => ({
      title: document.querySelector('#studioDetailTitle').textContent,
      toast: document.querySelector('#toast')?.textContent || '',
    }));
    check('失效的上次选择回到空状态并提示',
      invalidRestore.title === '自由输入' && invalidRestore.toast.includes('已不存在'),
      JSON.stringify(invalidRestore));

    // ============================================================
    // 32. 收藏：星标不触发行选择；筛选支持内置与自定义
    // ============================================================
    await selectPane('list');
    await page.click('#studioCats [data-studio-module="vs"]');
    await page.waitForTimeout(700);
    const detailBeforeFav = await page.evaluate(() => document.querySelector('#studioDetailTitle').textContent);
    await selectPane('list');
    const favWrap = page.locator('#studioList .studio-row-wrap').first();
    const favId = await favWrap.locator('.studio-row').getAttribute('data-studio-pick');
    await favWrap.locator('.studio-row-fav').click();
    await page.waitForTimeout(700);
    const favState = await page.evaluate(() => ({
      pressed: document.querySelector('#studioList .studio-row-fav')?.getAttribute('aria-pressed'),
      detail: document.querySelector('#studioDetailTitle').textContent,
      count: document.querySelectorAll('#studioList .studio-row').length,
      // 分类栏「收藏」后面的数字：点完星标必须当场同步，不能等切分类或刷新。
      catCount: document.querySelector('#studioCats [data-studio-module="fav"] .studio-cat-count')?.textContent,
    }));
    check('点星标只切收藏、不打开条目',
      favState.pressed === 'true' && favState.detail === detailBeforeFav && favState.count > 0,
      JSON.stringify({ ...favState, detailBeforeFav }));
    check('点星标后分类栏「收藏」数字立即同步', favState.catCount === '1', JSON.stringify(favState));
    check('收藏写入 settings.studio',
      settingsPosts.some((post) => Array.isArray(post.studio?.favorites)
        && post.studio.favorites.some((row) => row.kind === 'builtin' && row.id === favId)),
      JSON.stringify(settingsPosts.filter((post) => post.studio?.favorites).slice(-2)));

    await page.click('#studioCats [data-studio-module="fav"]');
    await page.waitForTimeout(700);
    const favCategory = await page.evaluate(() => ({
      label: document.querySelector('#studioCats [data-studio-module="fav"] .studio-cat-name')?.textContent,
      count: document.querySelector('#studioCats [data-studio-module="fav"] .studio-cat-count')?.textContent,
      rows: [...document.querySelectorAll('#studioList .studio-row')].map((row) => row.dataset.studioPick),
    }));
    check('分类栏有「收藏」且只显示已收藏的内置条目',
      favCategory.label === '收藏' && favCategory.count === '1'
      && favCategory.rows.length === 1 && favCategory.rows[0] === favId,
      JSON.stringify({ ...favCategory, favId }));

    await page.click('#studioList .studio-row-fav');
    await page.waitForTimeout(700);
    const unfav = await page.evaluate(() => ({
      rows: document.querySelectorAll('#studioList .studio-row').length,
      text: document.querySelector('#studioList').textContent,
      catCount: document.querySelector('#studioCats [data-studio-module="fav"] .studio-cat-count')?.textContent,
    }));
    check('在「收藏」里取消后行移除并显示空态',
      unfav.rows === 0 && unfav.text.includes('还没有收藏'), JSON.stringify(unfav));
    check('取消收藏后分类栏「收藏」数字立即归零', unfav.catCount === '0', JSON.stringify(unfav));

    await page.click('#studioCats [data-studio-module="custom"]');
    await page.waitForTimeout(700);
    check('自定义条目在列表里有删除入口',
      await page.evaluate(() => Boolean(document.querySelector('#studioList .studio-row-wrap.has-delete .studio-row-delete'))));
    await page.click('#studioList .studio-row-fav');
    await page.waitForTimeout(700);
    await page.click('#studioCats [data-studio-module="fav"]');
    await page.waitForTimeout(700);
    const customFav = await page.evaluate(() => ({
      rows: document.querySelectorAll('#studioList .studio-row').length,
      kind: document.querySelector('#studioList .studio-row')?.dataset.studioKind,
      pressed: document.querySelector('#studioList .studio-row-fav')?.getAttribute('aria-pressed'),
    }));
    check('「收藏」分类同时支持自定义条目',
      customFav.rows === 1 && customFav.kind === 'custom' && customFav.pressed === 'true',
      JSON.stringify(customFav));

    // ============================================================
    // 33. 定位模板库：清搜索/切分类/滚动高亮/失效提示
    // ============================================================
    await selectPane('list');
    await page.click('#studioCats [data-studio-module="vs"]');
    await page.waitForTimeout(700);
    check('预设条目没有删除入口（内置只读）',
      await page.evaluate(() => !document.querySelector('#studioList .studio-row-delete')));
    const locateRow = page.locator('#studioList .studio-row').nth(1);
    const locateId = await locateRow.getAttribute('data-studio-pick');
    await locateRow.click();
    await page.waitForTimeout(800);
    await selectPane('list');
    await page.fill('#studioSearch', 'zzzz-无匹配');
    await page.waitForTimeout(600);
    await selectPane('form');
    await page.click('#studioLocateEntry');
    await page.waitForTimeout(1100);
    const located = await page.evaluate((id) => {
      const row = document.querySelector(`#studioList [data-studio-pick="${id}"]`);
      return {
        query: document.querySelector('#studioSearch').value,
        highlighted: Boolean(row?.classList.contains('studio-row-locating')),
        visible: Boolean(row),
        pane: document.querySelector('#studioView').dataset.pane,
      };
    }, locateId);
    check('参数区定位会清空搜索、切回模板档并高亮目标行',
      located.visible && located.highlighted && located.query === ''
      && (!isMobile || located.pane === 'list'),
      JSON.stringify({ ...located, locateId }));

    // 从生成结果定位：结果属于别的分类时自动切分类
    taskList[0].input = { ...(taskList[0].input || {}), item_id: locateId, custom_id: '', module: 'vs' };
    await selectPane('list');
    await page.click('#studioCats [data-studio-module="global"]');
    await page.waitForTimeout(700);
    await openHistory();
    await page.click(`#studioHistoryList [data-studio-task="${TASK_ID}"]`);
    await page.waitForTimeout(700);
    check('结果里关联模板时「定位模板」可用',
      await page.evaluate(() => !document.querySelector('#studioLocateResult').hidden));
    await page.click('#studioLocateResult');
    await page.waitForTimeout(1200);
    const resultLocated = await page.evaluate((id) => ({
      module: document.querySelector('#studioCats [aria-pressed="true"]')?.dataset.studioModule,
      highlighted: Boolean(document.querySelector(`#studioList [data-studio-pick="${id}"]`)?.classList.contains('studio-row-locating')),
      active: Boolean(document.querySelector(`#studioList [data-studio-pick="${id}"]`)?.classList.contains('active')),
      title: document.querySelector('#studioDetailTitle')?.textContent || '',
      pane: document.querySelector('#studioView').dataset.pane,
    }), locateId);
    check('从结果定位会切到对应分类、选中并高亮',
      resultLocated.module === 'vs' && resultLocated.highlighted && resultLocated.active
      && resultLocated.title.length > 0
      && (!isMobile || resultLocated.pane === 'form'),
      JSON.stringify(resultLocated));

    taskList[0].input = { ...(taskList[0].input || {}), item_id: 'no-such-entry', custom_id: '', module: 'vs' };
    await openHistory();
    await page.click(`#studioHistoryList [data-studio-task="${TASK_ID}"]`);
    await page.waitForTimeout(600);
    await page.click('#studioLocateResult');
    await page.waitForTimeout(800);
    check('定位已不存在的模板会给出可理解提示', await page.evaluate(() =>
      (document.querySelector('#toast')?.textContent || '').includes('已不存在')));

    // ============================================================
    // 34. 上传图片 + 视觉反推 + 带图生成
    // ============================================================
    await selectPane('form');
    await page.click('#studioImagePick');
    await page.waitForTimeout(400);
    check('上传按钮打开多图弹窗',
      await page.evaluate(() => document.querySelector('#studioImageDialog').open));

    uploadFailuresLeft = 1;
    await page.setInputFiles('#studioImageFileInput', { name: 'first.png', mimeType: 'image/png', buffer: PIXEL_PNG });
    await page.waitForTimeout(800);
    const failedUpload = await page.evaluate(() => ({
      rows: document.querySelectorAll('#studioImageQueue .studio-image-queue-row').length,
      error: document.querySelector('#studioImageQueue .studio-image-queue-info small')?.textContent || '',
      retry: Boolean(document.querySelector('#studioImageQueue [data-studio-image-retry]')),
    }));
    check('单张上传失败保留在列表并可重试',
      failedUpload.rows === 1 && failedUpload.error.includes('上传失败') && failedUpload.retry,
      JSON.stringify(failedUpload));
    await page.click('#studioImageQueue [data-studio-image-retry]');
    await page.waitForTimeout(800);
    check('重试后该图片就绪', await page.evaluate(() =>
      !document.querySelector('#studioImageQueue [data-studio-image-retry]')
      && (document.querySelector('#studioImageQueue .studio-image-queue-info small')?.textContent || '').includes('已就绪')));

    uploadDelayMs = 700;
    await page.setInputFiles('#studioImageFileInput', { name: 'second.png', mimeType: 'image/png', buffer: PIXEL_PNG });
    await page.waitForTimeout(140);
    check('上传进行中禁用「确定」',
      await page.evaluate(() => document.querySelector('#studioImageConfirm').disabled));
    await page.waitForTimeout(1000);
    uploadDelayMs = 0;

    await page.evaluate(() => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'drop.png', { type: 'image/png' }));
      document.querySelector('#studioImageDrop').dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: transfer }));
    });
    await page.waitForTimeout(900);
    check('拖拽可追加图片',
      await page.evaluate(() => document.querySelectorAll('#studioImageQueue .studio-image-queue-row').length) === 3);

    await page.click('#studioImageQueue .studio-image-queue-remove');
    await page.waitForTimeout(300);
    await page.click('#studioImageConfirm');
    await page.waitForTimeout(700);
    const confirmed = await page.evaluate(() => ({
      dialogOpen: document.querySelector('#studioImageDialog').open,
      previews: document.querySelectorAll('#studioImagePreview .studio-image-chip').length,
      visionVisible: !document.querySelector('#studioVisionAnalyze').hidden,
    }));
    check('确定后关闭弹窗并在输入区显示缩略图',
      !confirmed.dialogOpen && confirmed.previews === 2 && confirmed.visionVisible,
      JSON.stringify(confirmed));

    const inputBeforeVision = await page.inputValue('#studioUserRequest');
    visionMode = 'fail';
    await page.click('#studioVisionAnalyze');
    await page.waitForTimeout(800);
    const visionFailed = await page.evaluate(() => ({
      input: document.querySelector('#studioUserRequest').value,
      toast: document.querySelector('#toast')?.textContent || '',
    }));
    check('视觉失败如实报错且不改写输入',
      visionFailed.toast.includes('图片识别失败') && visionFailed.input === inputBeforeVision,
      JSON.stringify({ toast: visionFailed.toast }));

    visionMode = 'partial';
    await page.click('#studioVisionAnalyze');
    await page.waitForTimeout(800);
    const visionPartial = await page.evaluate(() => ({
      input: document.querySelector('#studioUserRequest').value,
      toast: document.querySelector('#toast')?.textContent || '',
    }));
    check('视觉部分失败保留图1结果并说明图2失败',
      visionPartial.input.includes('【图1】') && !visionPartial.input.includes('【图2】')
      && visionPartial.toast.includes('1 张失败') && visionPartial.toast.includes('图2'),
      JSON.stringify({ toast: visionPartial.toast }));

    visionMode = 'ok';
    await page.click('#studioVisionAnalyze');
    await page.waitForTimeout(800);
    const visionOk = await page.evaluate(() => ({
      input: document.querySelector('#studioUserRequest').value,
      status: document.querySelector('#studioImageStatus').textContent,
    }));
    check('视觉成功按图1/图2 写入可编辑输入',
      visionOk.input.includes('【参考图片识别结果】')
      && visionOk.input.includes('【图1】') && visionOk.input.includes('【图2】')
      && visionOk.status.includes('2 张参考图片'),
      JSON.stringify({ status: visionOk.status, tail: visionOk.input.slice(-80) }));
    check('视觉请求按顺序携带结构化图片引用',
      visionCalls.length > 0
      && visionCalls.at(-1).images.length === 2
      && String(visionCalls.at(-1).images[0].path).includes('smoke-'),
      JSON.stringify(visionCalls.at(-1) || {}));
    check('视觉请求优先携带当前参数模型',
      visionCalls.length > 0
      && visionCalls.at(-1).model_key === 'online:probe-te'
      && visionCalls.at(-1).model_name === 'probe-default',
      JSON.stringify({ model_key: visionCalls.at(-1)?.model_key, model_name: visionCalls.at(-1)?.model_name }));

    await page.fill('#studioUserRequest', '用这两张图生成一张海报');
    await page.click('#studioGenerate');
    await page.waitForTimeout(1100);
    const imageGenerate = generateCalls.at(-1) || {};
    check('生成请求按图1/图2 顺序携带图片引用',
      Array.isArray(imageGenerate.images) && imageGenerate.images.length === 2
      && Boolean(imageGenerate.images[0].thumb_path),
      JSON.stringify(imageGenerate));
    check('Studio 图片不进入聊天待发送附件', await page.evaluate(async () => {
      const mod = await import('/js/01-core.js');
      return mod.state.pendingFiles.length === 0;
    }));

    await selectPane('form');
    await page.click('#studioImagePreview .studio-image-chip .studio-image-remove');
    await page.waitForTimeout(300);
    check('输入区可移除单张图片', await page.evaluate(() =>
      document.querySelectorAll('#studioImagePreview .studio-image-chip').length) === 1);

    for (const err of errors) failures.push(`console: ${err}`);
  } catch (e) {
    failures.push(`exception: ${e.message}`);
  } finally {
    await page.close();
  }
  return { name: vp.name, failures };
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
      console.log(`[${r.name}] 失败项: ${JSON.stringify(r.failures)}`);
    } else {
      console.log(`[${r.name}] 全部通过`);
    }
  }
  process.exit(failed ? 1 : 0);
})();
