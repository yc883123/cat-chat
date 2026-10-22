// 工作台（Studio）前端冒烟：桌面 + 375px 手机视口。
// 断言：侧栏「工作台」是唯一入口、重复点不退出；纵向分类带计数；列表可搜索；
// 选中条目出现详情/变量/正文预览；「我的输入」是一级输入位；
// 侧栏「历史产出」打开完整列表（0/1/3/60 条口径、字段回退）、点行显示结果；
// 任务动作在结果档可用；手机三档按定义切换；追问框初始无选中且「创建会话并继续」禁用、
// 取消只关闭不创建会话；无 pageerror / console.error。
// 另含：桌面侧栏收起后的恢复入口、任务/自定义条目迟到响应、离开期间任务终结后重进恢复、
// 搜索过滤、自由输入生成、取消接口契约。
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
  // 迟到响应保护：这些任务 id 的详情接口延迟返回，用于验证快速切换不串台。
  const slowDetailMs = new Map();
  // 自定义条目列表接口的延迟（毫秒）：用于验证自定义条目迟到加载不覆盖新选择。
  let customDelayMs = 0;
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
  await page.route('**/api/studio/handoff', async (route) => {
    handoffCalls.push(JSON.parse(route.request().postData() || '{}'));
    return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: '冒烟注入：不应触达交接接口' }) });
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
    await openDrawerIfMobile();
    await page.click('#studioNavHistory');
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
    check('工作台顶层分页为「模块库」（可扩展 tab）',
      isolation.pages.length === 1 && isolation.pages[0] === '模块库' && !isolation.hasApiSelect,
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

    // 9. 标题区最近任务最多 3 条（接口注入了 6 条）
    const recent = await page.evaluate(() => {
      const chips = [...document.querySelectorAll('#studioTaskHistory .studio-task-row')];
      return { count: chips.length, labels: chips.map((el) => el.textContent.trim()) };
    });
    check('最近任务 chips 不超过 3 条', recent.count > 0 && recent.count <= 3, JSON.stringify(recent));

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

    // 19. 侧栏工作台导航只有「历史产出」（模板入口已按用户要求去掉）
    const nav = await page.evaluate(() => ({
      labels: [...document.querySelectorAll('#sidebarStudioNav .sidebar-studio-link')].map((el) => el.textContent.trim()),
    }));
    check('侧栏工作台导航只保留「历史产出」',
      nav.labels.length === 1 && nav.labels[0] === '历史产出', JSON.stringify(nav));

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
      lateGuard.historyHidden
      && lateGuard.stream.includes('生成结果正文')
      && !lateGuard.stream.includes('第二条结果正文'),
      JSON.stringify({ stream: lateGuard.stream.slice(0, 40) }));
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
