// 「流式回复撞上点击任务面板 → 消息区整体重渲染」回归冒烟（由 verify/stream_rerender_smoke.py 编排）。
//
// 覆盖：
//   ① 面板那一行的「跳转」按钮点了会跳会话（切会话入口已从"整卡可点"收窄为显式按钮）；
//   ② 点完之后**同一条气泡还在文档里**（`replaceChildren` 后必须复挂活动流的 run 行）——
//      改前这一步会失败：正在流式输出的那条助手气泡被连根拔掉且无人复挂；
//   ③ 点完之后后续 delta 仍继续落进那条气泡（流没断、渲染目标没换人）；
//   ④ 跑完后后端只有一条助手回复、正文完整（没有出现重复气泡/丢内容）；
//   ⑤ 零页面错误。
//
// 全真链路：真 POST /api/chat、真 run、真事件流；只有模型响应来自本地假 SSE 服务
// （10 段、每段 400ms，好让「流到一半点跳转按钮」稳定复现）。任务面板那一行由浏览器侧
// 拦截 /api/tasks 注入（面板只列后台作业，点「跳转」即 openConversation(当前会话)）。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8798';
const BOUND_MODEL = process.env.NAIBA_SMOKE_BOUND_MODEL || 'smoke-slow-model';
const Q1 = process.env.NAIBA_SMOKE_Q1 || '流式重渲染冒烟：先给历史';
const A1 = process.env.NAIBA_SMOKE_A1 || '第一答：历史内容。';
const SEND = process.env.NAIBA_SMOKE_SEND || '点任务行之后写的那条消息';
const FIRST_DELTA = process.env.NAIBA_SMOKE_FIRST_DELTA || '先说结论：';
const LAST_DELTA = process.env.NAIBA_SMOKE_LAST_DELTA || '（完）';
const BG_TASK_ID = 'smoke-bg-task-1';

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(path, options = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(`${BASE}${path}`, init);
  return response.json().catch(() => ({}));
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const naibaLogs = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    const text = msg.text();
    if (text.includes('[naiba]')) naibaLogs.push(text);
    if (msg.type() === 'error') pageErrors.push(`console.error: ${text}`);
  });

  const endOf = () => Date.now() + 15000;
  async function waitFor(fn, timeout = 15000, step = 100) {
    const end = Date.now() + timeout;
    let last = null;
    while (Date.now() < end) {
      last = await fn();
      if (last) return last;
      await page.waitForTimeout(step);
    }
    return last;
  }

  // 会话只在启动时自动打开一个（seeded），先拿 id 再装任务注入路由。
  const list = await apiJson('/api/conversations');
  const convId = String(((list.conversations || [])[0] || {}).id || '');

  // 发送前置：模型目录（不真连供应商）。
  await page.route('**/api/providers/models', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ models: [{ id: BOUND_MODEL, name: BOUND_MODEL }] }),
  }));

  // 任务面板：注入一个「正在运行」的后台作业（面板只列 jobs_only，故用 comfyui 类型）。
  // 任务详情/日志接口也要拦住，否则面板展开时会去打真实接口（401/404 会污染页面错误）。
  await page.route('**/api/tasks**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      tasks: [{
        id: BG_TASK_ID,
        kind: 'comfyui',
        status: 'running',
        conversation_id: convId,
        message: 'ComfyUI 批量生成（冒烟）',
        parent_job_id: '',
        created_at: Date.now() - 5000,
        updated_at: Date.now(),
        detail: {},
      }],
    }),
  }));
  await page.route('**/api/jobs/**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ events: [], cursor: 0 }),
  }));

  const markedRow = () => page.evaluate(() => {
    const row = document.querySelector('#messages [data-smoke-live="1"]');
    return row
      ? { inMessages: Boolean(row.closest('#messages')), text: row.textContent || '',
          runId: row.dataset.runId || '' }
      : null;
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 25000 });
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 25000 });
    await page.waitForTimeout(600);
    const history = await page.locator('#messages .message-row').count();
    check('会话就绪（历史 2 条）', history === 2, `实际 ${history}`);

    const panelRow = await waitFor(
      () => page.locator(`#taskList [data-task-id="${BG_TASK_ID}"]`).count().then((n) => n > 0),
      10000,
    );
    check('任务面板里出现那一行后台作业', Boolean(panelRow));

    // ---- 发送：真 run + 真事件流（模型侧是本地假 SSE）----
    await page.fill('#messageInput', SEND);
    await page.click('#sendButton');
    const live = await waitFor(async () => {
      const found = await page.evaluate((wanted) => {
        const row = [...document.querySelectorAll('#messages .message-row.assistant')]
          .find((el) => el.textContent.includes(wanted) && !el.dataset.messageId);
        if (!row) return null;
        row.dataset.smokeLive = '1';
        return { runId: row.dataset.runId || '', text: row.textContent || '' };
      }, FIRST_DELTA);
      return found;
    }, 20000, 80);
    check('流式气泡已出现并收到第一段正文', Boolean(live), JSON.stringify(live));
    if (!live) throw new Error('活动流的气泡没出现，后续断言无从谈起');

    // ---- 核心：流到一半点任务面板那一行的「跳转」按钮 ----
    // （切会话入口已从「整卡可点」收窄为显式按钮：卡片空白不再有副作用，
    //   但按钮走的是同一个 openConversation(当前会话) → 重渲染路径，触发条件不变。）
    const rendersBefore = naibaLogs.filter((line) => line.includes('renderMessages')).length;
    await page.click('#openTasks');
    await page.waitForSelector(`#taskList [data-task-id="${BG_TASK_ID}"] [data-task-open]`, { timeout: 5000 });
    await page.click(`#taskList [data-task-id="${BG_TASK_ID}"] [data-task-open]`);

    const reRendered = await waitFor(
      () => Promise.resolve(
        naibaLogs.filter((line) => line.includes('renderMessages')).length > rendersBefore,
      ),
      8000,
    );
    check('点击「跳转」按钮触发了消息区重渲染（缺陷的触发条件成立）', Boolean(reRendered),
      `renderMessages 日志条数=${naibaLogs.filter((l) => l.includes('renderMessages')).length}`);

    const afterClick = await markedRow();
    check('重渲染后同一条气泡仍在消息区里（回复没有从屏幕上消失）',
      Boolean(afterClick && afterClick.inMessages),
      JSON.stringify(afterClick));
    check('重渲染没有清掉已收到的正文',
      Boolean(afterClick && afterClick.text.includes(FIRST_DELTA)),
      JSON.stringify(afterClick));

    // ---- 点完之后流还在往那条气泡里写 ----
    const grew = await waitFor(async () => {
      const row = await markedRow();
      return row && row.text.includes(LAST_DELTA) ? row : null;
    }, 20000, 120);
    check('点完之后后续 delta 仍继续落进同一条气泡（流没被拆掉）',
      Boolean(grew), JSON.stringify(await markedRow()));

    // ---- 完成后：后端只有一条助手回复，正文完整 ----
    const finished = await waitFor(async () => {
      const data = await apiJson(`/api/conversations/${encodeURIComponent(convId)}`);
      const assistant = (data.messages || []).filter((m) => m.role === 'assistant');
      return assistant.length === 2 ? assistant[assistant.length - 1] : null;
    }, 25000, 300);
    check('后端落库一条（且只有一条）本轮助手回复',
      Boolean(finished), JSON.stringify(finished && finished.content));
    check('落库正文包含首尾两段（流内容没丢）',
      Boolean(finished && finished.content.includes(FIRST_DELTA) && finished.content.includes(LAST_DELTA)),
      String(finished && finished.content));

    await page.waitForTimeout(800);
    const visible = await page.evaluate((wanted) => {
      const row = [...document.querySelectorAll('#messages .message-row.assistant')]
        .find((el) => el.textContent.includes(wanted));
      return row ? { rows: document.querySelectorAll('#messages .message-row.assistant').length,
                     text: row.textContent.slice(0, 60) } : null;
    }, LAST_DELTA);
    check('完成后界面上能看到这条完整回复', Boolean(visible), JSON.stringify(visible));

    check('页面无 JS 错误', pageErrors.length === 0, pageErrors.join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, error && error.message ? error.message : String(error));
    try {
      const dump = await page.evaluate(() => ({
        rows: [...document.querySelectorAll('#messages .message-row')].map((row) => ({
          id: row.dataset.messageId || '',
          runId: row.dataset.runId || '',
          smoke: row.dataset.smokeLive || '',
          cls: row.className,
          text: (row.textContent || '').replace(/\s+/g, ' ').slice(0, 40),
        })),
        taskRows: document.querySelectorAll('#taskList [data-task-id]').length,
        dialogOpen: Boolean(document.querySelector('#tasksDialog[open]')),
      }));
      console.log(`\n[DEBUG 现场] ${JSON.stringify(dump, null, 2)}`);
      console.log(`[DEBUG naiba 日志] ${naibaLogs.slice(-6).join(' || ')}`);
      console.log(`[DEBUG 页面错误] ${pageErrors.join(' | ') || '（无）'}`);
    } catch (_) { /* 求值失败时忽略，至少保住原报错 */ }
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\n${failures.length} 项未通过` : '\n全部通过');
  process.exit(failures.length ? 1 : 0);
})();
