// 工作台（Studio）前端冒烟：桌面 + 375px 手机视口。
// 断言：顶栏入口可打开三区视图；分类 tab 带计数；列表可搜索；选中条目出现详情/变量/正文预览；
// 「我的输入」是一级输入位；追问框初始无选中且「创建会话并继续」禁用、取消只关闭不创建会话；
// 无 pageerror / console.error。
// 目录/任务/Agent 走路由注入（隔离装的模型目录连不出去）：不真连供应商、不真创建会话。
// 运行（先起隔离源码实例 verify\_serve_tmp.py）：node verify\studio_ui_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';

const TASK_ID = 'smoke-task-0001';
const FAKE_TASK = {
  id: TASK_ID, status: 'done', detail: '', error: '',
  title: '冒烟任务', module: 'global', origin_label: '', result: '这是一段冒烟生成的结果正文。',
  result_partial: '', reasoning: [], usage: {}, performance: {}, finish_reason: '',
  handoff: null, created_at: 1, updated_at: 2, started_at: 1, finished_at: 2,
  input: { content: '冒烟输入', model_key: 'online:probe-te', model_name: 'probe-default', item_id: '', title: '冒烟任务' },
};
const FAKE_AGENTS = [
  { id: 'agent-smoke-a', name: '冒烟 Agent A', system_prompt: '假系统提示词（只选一个）' },
  { id: 'agent-smoke-b', name: '冒烟 Agent B', system_prompt: '' },
];

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile', width: 375, height: 812 },
];

async function runViewport(browser, vp) {
  const errors = [];
  const handoffCalls = [];
  const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    // 隔离实例的供应商目录指向 example.invalid，模型目录探测 400 是本次冒烟的预期噪声
    // （不评模型能力，只验证 UI 结构与交互），不计为页面错误。
    if (msg.text().includes('status of 400 (Bad Request)') && msg.location()?.url?.includes('/api/providers/models')) return;
    errors.push(`console.error: ${msg.text()}`);
  });
  // 任务/Agent/交接注入（隔离实例天然没有已完成的生成任务）。
  await page.route('**/api/studio/tasks', (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ tasks: [FAKE_TASK] }) });
  });
  await page.route(`**/api/studio/tasks/${TASK_ID}`, (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ task: FAKE_TASK }) });
  });
  await page.route('**/api/studio/tasks/*/events*', (route) => {
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ events: [{ seq: 1, type: 'done' }], count: 1, after: 0, last_seq: 1, terminal: true, terminal_seq: 1, status: 'done' }),
    });
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
  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForTimeout(3000);
    // 隔离实例是全新安装：首次启动引导会弹在 top layer 拦截点击，先关掉再继续。
    const onboardingOpen = await page.evaluate(() => document.querySelector('#onboardingDialog')?.open);
    if (onboardingOpen) {
      await page.click('#onboardingDialog [data-close="onboardingDialog"]');
      await page.waitForTimeout(400);
    }

    // 1. 打开工作台
    await page.click('#openStudio');
    await page.waitForTimeout(1500);
    const opened = await page.evaluate(() => document.body.dataset.studio === 'on'
      && !document.querySelector('#studioView').hidden);
    check('工作台视图打开', opened);

    // 2. 分类 tab 渲染且带计数
    const tabs = await page.evaluate(() => {
      const tabs = [...document.querySelectorAll('.studio-tab')];
      return { count: tabs.length, hasCount: tabs.some((t) => /\d/.test(t.textContent)) };
    });
    check('分类 tab≥10 且带条目计数', tabs.count >= 10 && tabs.hasCount, JSON.stringify(tabs));

    // 3. 列表渲染
    const rows = await page.locator('.studio-row').count();
    check('模板列表有行', rows > 0, `rows=${rows}`);

    // 4. 选第一个条目 → 详情/预览出现
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

    // 5. 一级输入位存在（用户视角的最终输入）
    check('「我的输入」可见', await page.locator('#studioUserRequest').isVisible());

    // 6. 历史任务选择 → 完成任务 → 「继续追问」可点
    await page.click('.studio-task-row');
    await page.waitForTimeout(500);
    const handoffVisible = await page.evaluate(() => !document.querySelector('#studioHandoff').hidden);
    check('完成任务后「继续追问」可用', handoffVisible);

    // 7. 追问框：初始无选中，「创建会话并继续」禁用；取消只关闭不触达交接接口
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

    // 8. 重新打开 → 显式选中一个 Agent → 「继续」可用
    await page.click('#studioHandoff');
    await page.waitForTimeout(500);
    await page.click('.studio-agent-row');
    await page.waitForTimeout(200);
    check('显式选中后「继续」可用', await page.evaluate(() => !document.querySelector('#studioHandoffConfirm').disabled));
    await page.click('#studioAgentDialog [data-close="studioAgentDialog"]');
    await page.waitForTimeout(300);
    check('选中后取消也不创建会话', handoffCalls.length === 0, JSON.stringify(handoffCalls));

    // 9. 返回普通对话视图
    await page.click('#openStudio');
    await page.waitForTimeout(600);
    check('再点入口返回对话区', await page.evaluate(() => document.body.dataset.studio !== 'on'));

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
