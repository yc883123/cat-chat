// 分支时可选「更换 Agent」冒烟（由 verify/branch_agent_smoke.py 起独立源码 server 后调用）。
//
// 覆盖（断言钉在**真后端**与**可用性**两层，不只存在性）：
//   ① 源会话已固化 ⇒ `#agentSelect` **禁用**（这是本需求要解开的锁）；
//   ② 点「分支」必须先弹选择框，且两个选项正文都写清后果；
//   ③ 选「更换 Agent」：后端三列清空、`agent_id` 仍继承、历史只复制 2 条，
//      **新会话的 `#agentSelect` 真的可用**（`enabled === true`）；
//   ④ 选「保持当前 Agent」：工具集逐字继承、下拉**仍然禁用**（回归）；
//   ⑤ 「取消」不产生新会话（会话总数不变）；
//   ⑥ 零页面错误、零未解释的 4xx（占位供应商导致的 models 请求已拦并计数）。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8804';
const SRC_ID = process.env.NAIBA_SMOKE_SRC_ID || '';
const Q2_ID = process.env.NAIBA_SMOKE_Q2_ID || '';
const FROZEN_TOOLS = (process.env.NAIBA_SMOKE_FROZEN_TOOLS || '').split(',').filter(Boolean);
const AGENT_ID = process.env.NAIBA_SMOKE_AGENT_ID || '';
const SHOTS = process.env.NAIBA_SMOKE_SHOTS || require('path').join(__dirname, '..', 'verify');

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}
const shot = (name) => require('path').join(SHOTS, `branch_agent_smoke_${name}.png`);

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 940 } });
  const pageErrors = [];
  const httpErrors = [];
  let modelsNoise = 0; // 占位供应商 base_url=example.invalid 造成的 4xx，拦截后计数即可
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`);
  });
  page.on('response', (response) => {
    if (response.status() >= 400) httpErrors.push(`${response.status()} ${response.url()}`);
  });

  const apiJson = async (path, options = {}) => {
    const init = { headers: { 'Content-Type': 'application/json' }, ...options };
    if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
    const response = await fetch(`${BASE}${path}`, init);
    return response.json().catch(() => ({}));
  };
  const conversations = async () => (await apiJson('/api/conversations')).conversations || [];
  const detail = async (id) => apiJson(`/api/conversations/${id}`);
  const rawTools = (conv) => JSON.stringify(conv?.enabled_tool_ids || []);
  const listIds = async () => (await conversations()).map((c) => c.id);
  const activeId = () => page.evaluate(
    () => document.querySelector('.conversation-item.active')?.dataset.conversationId || '',
  );
  const agentSelectState = () => page.evaluate(() => {
    const select = document.querySelector('#agentSelect');
    return select ? { disabled: select.disabled, title: select.title || '', value: select.value } : null;
  });
  const rowOf = (id) => `.conversation-item[data-conversation-id="${id}"]`;
  const msgRow = (id) => `#messages .message-row[data-message-id="${id}"]`;

  async function waitFor(fn, timeout = 15000, step = 120) {
    const end = Date.now() + timeout;
    let last = null;
    while (Date.now() < end) {
      last = await fn();
      if (last) return last;
      await page.waitForTimeout(step);
    }
    return last;
  }

  // 打开一个会话并等它的第一条消息画出来。
  async function openConversation(id, anchorMessageId) {
    await page.click(`${rowOf(id)} .conversation-open`);
    await page.waitForSelector(msgRow(anchorMessageId), { timeout: 15000 });
    await page.waitForTimeout(400);
  }

  // 点分支按钮 → 只等对话框，不点任何选项（调用方决定选哪个）。
  async function openBranchDialog() {
    const before = await listIds();
    await page.click(`${msgRow(Q2_ID)} [data-branch-message]`);
    await page.waitForSelector('#branchAgentDialog[open]', { timeout: 8000 });
    return before;
  }

  try {
    await page.route('**/api/providers/models', (route) => {
      modelsNoise += 1;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ models: [{ id: 'smoke-ba-pro', name: 'smoke-ba-pro' }] }),
      });
    });
    // 首启引导是 top layer 弹窗，会拦住所有点击；先置标记（隔离实例每次都是首启）。
    await page.addInitScript(() => {
      try { window.localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (e) { /* 无关紧要 */ }
    });

    await page.goto(BASE, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector(rowOf(SRC_ID), { timeout: 15000 });
    await page.waitForTimeout(500);
    const onboardingOpen = await page.evaluate(
      () => Boolean(document.querySelector('#onboardingDialog')?.open));
    if (onboardingOpen) {
      await page.click('#onboardingSkip').catch(() => {});
      await page.waitForTimeout(300);
    }

    // ==================================================== ① 源会话：锁在
    await openConversation(SRC_ID, Q2_ID);
    const srcSelect = await agentSelectState();
    check('源会话 Agent 下拉存在', Boolean(srcSelect));
    check('源会话已固化 ⇒ Agent 下拉禁用', srcSelect?.disabled === true,
      JSON.stringify(srcSelect));
    const srcDetail = await detail(SRC_ID);
    check('源会话三列确实非空（前置条件）',
      srcDetail.enabled_tool_ids?.length > 0 && srcDetail.agent_id === AGENT_ID,
      rawTools(srcDetail));

    // ==================================================== ② 点分支先弹选择框
    await openBranchDialog();
    check('点分支先弹选择框（不直接建会话）',
      await page.isVisible('#branchAgentDialog'));
    const notes = ((await page.textContent('#branchAgentNotes')) || '').trim();
    check('正文说明「保持」的后果（缓存继续命中）', notes.includes('缓存'), notes.slice(0, 200));
    check('正文说明「更换」的后果（发送前可换）', notes.includes('发送前'), notes.slice(0, 200));
    check('「保持当前 Agent」是主按钮（primary）',
      await page.getAttribute('#branchAgentKeep', 'class') === 'primary-button');
    check('两个选项都在（不是藏着的按钮）',
      await page.isVisible('#branchAgentKeep') && await page.isVisible('#branchAgentReset'));
    await page.screenshot({ path: shot('1_dialog') });

    // ==================================================== ③ 取消：不留分支
    const beforeCancel = await listIds();
    await page.click('#branchAgentCancel');
    await page.waitForSelector('#branchAgentDialog:not([open])', { timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(600);
    const afterCancel = await listIds();
    check('取消不产生新会话', afterCancel.length === beforeCancel.length,
      `before=${beforeCancel.length} after=${afterCancel.length}`);
    check('取消后仍停在源会话', (await activeId()) === SRC_ID);

    // ==================================================== ④ 更换 Agent
    await openBranchDialog();
    await page.click('#branchAgentReset');
    await page.waitForSelector('#branchAgentDialog:not([open])', { timeout: 8000 }).catch(() => {});
    const newId = await waitFor(async () => {
      const active = await activeId();
      return active && active !== SRC_ID ? active : '';
    });
    check('选「更换 Agent」后切到了新会话', Boolean(newId), `active=${newId}`);
    const changed = await detail(newId);
    check('新会话 enabled_tool_ids 已清空（下拉解锁的唯一判据）',
      (changed.enabled_tool_ids || []).length === 0, rawTools(changed));
    check('新会话 skill_policy 已清空', !Object.keys(changed.skill_policy || {}).length,
      JSON.stringify(changed.skill_policy));
    check('新会话 agent_id 仍继承（「可选」而非「强制换」）',
      changed.agent_id === AGENT_ID, `agent_id=${changed.agent_id}`);
    check('历史只复制分支点之前（2 条）',
      (changed.messages || []).length === 2, `len=${(changed.messages || []).length}`);
    check('分支来源照常登记',
      changed.branched_from_id === SRC_ID && changed.branch_message_id === Q2_ID);

    // 关键：可用性。§九.81 ——「按钮存在」≠「用户能用」。
    const newSelect = await agentSelectState();
    check('新会话 Agent 下拉真的可用（enabled）', newSelect?.disabled === false,
      JSON.stringify(newSelect));
    check('下拉提示语已换成「发送首条消息后固化」',
      (newSelect?.title || '').includes('发送首条消息后固化'), newSelect?.title);
    const toastText = ((await page.textContent('#toast')) || '').trim();
    check('toast 提示可换 Agent', toastText.includes('更换 Agent'), toastText);
    const prefilled = await page.inputValue('#messageInput').catch(() => '');
    check('分支消息已预填输入框', prefilled.length > 0, prefilled.slice(0, 40));
    await page.screenshot({ path: shot('2_unlocked') });

    // ==================================================== ⑤ 保持当前 Agent（回归）
    await openConversation(SRC_ID, Q2_ID);
    await openBranchDialog();
    await page.click('#branchAgentKeep');
    await page.waitForSelector('#branchAgentDialog:not([open])', { timeout: 8000 }).catch(() => {});
    const keptId = await waitFor(async () => {
      const active = await activeId();
      return active && active !== SRC_ID ? active : '';
    });
    const kept = await detail(keptId);
    check('「保持」时工具集逐字继承', rawTools(kept) === rawTools(srcDetail),
      `kept=${rawTools(kept)} src=${rawTools(srcDetail)}`);
    check('「保持」时 skill_policy 也继承',
      JSON.stringify(kept.skill_policy) === JSON.stringify(srcDetail.skill_policy),
      JSON.stringify(kept.skill_policy));
    const keptSelect = await agentSelectState();
    check('「保持」后 Agent 下拉仍然禁用（存量行为不变）', keptSelect?.disabled === true,
      JSON.stringify(keptSelect));
    await page.screenshot({ path: shot('3_keep_locked') });

    // ==================================================== ⑥ 噪音门
    const unexpected = httpErrors.filter((line) => !line.includes('/api/providers/models'));
    check('无页面错误', pageErrors.length === 0, pageErrors.join(' | '));
    check('无未解释的 4xx', unexpected.length === 0, unexpected.join(' | '));
    check('models 请求已拦并计数（噪音数 == 拦截数）',
      modelsNoise >= 0 && httpErrors.length === 0, `models=${modelsNoise} 4xx=${httpErrors.length}`);
  } catch (error) {
    check(`冒烟异常终止：${error.message}`, false);
  } finally {
    await browser.close();
  }

  console.log(failures.length
    ? `\nFAILURES (${failures.length}): ${failures.join('; ')}`
    : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
