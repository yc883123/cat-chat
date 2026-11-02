// Feedback gate: isolated real App/HTTP, loopback model, real ToolExecutor.
// No production port, external models, mocked application routes or DB edits.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const ROOT = path.resolve(__dirname, '..');
const runRoot = fs.mkdtempSync(path.join(__dirname, '_super-feedback-'));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let fixture, server, browser, serverLog = '';
async function json(url, body, headers = {}) {
  const response = await fetch(url, { method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', ...headers }, body: body === undefined ? undefined : JSON.stringify(body) });
  const payload = await response.json();
  assert.ok(response.ok, `${response.status} ${url}: ${JSON.stringify(payload)}`);
  return payload;
}
const api = (url, body) => json(fixture.base + url, body);
const control = (body) => json(fixture.control_base + '/__fixture/control', body, { 'X-Fixture-Token': fixture.control_token });
async function until(work, description, timeout = 20000) {
  const deadline = Date.now() + timeout;
  let result;
  while (Date.now() < deadline) { result = await work(); if (result) return result; await sleep(100); }
  throw new Error(`Timed out: ${description}; last=${JSON.stringify(result)}`);
}
async function start() {
  server = spawn(path.join(ROOT, '.venv', 'Scripts', 'python.exe'),
    [path.join(__dirname, '_serve_super_agent_v1_ui.py'), '--root', path.join(runRoot, 'isolated')],
    { cwd: ROOT, env: { ...process.env, PYTHONIOENCODING: 'utf-8', NO_PROXY: 'localhost,127.0.0.1,::1' }, stdio: ['ignore', 'pipe', 'pipe'] });
  server.stdout.on('data', (chunk) => { serverLog += chunk; });
  server.stderr.on('data', (chunk) => { serverLog += chunk; });
  await until(async () => {
    if (server.exitCode !== null) throw new Error(serverLog);
    const file = path.join(runRoot, 'isolated', 'fixture.json');
    if (!fs.existsSync(file)) return false;
    fixture = JSON.parse(fs.readFileSync(file, 'utf8'));
    return (await fetch(fixture.base)).ok;
  }, 'fixture ready', 45000);
}
async function stop() {
  if (server && server.exitCode === null) {
    const exited = new Promise((resolve) => server.once('exit', resolve));
    if (fixture) await control({ operation: 'shutdown' }).catch(() => {});
    await Promise.race([exited, sleep(5000)]);
    if (server.exitCode === null) server.kill();
  }
  fs.writeFileSync(path.join(runRoot, 'server.log'), serverLog, 'utf8');
}
function guard(page) {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    if (m.location().url === fixture.base + '/favicon.ico' && /^Failed to load resource:.*404/.test(m.text())) return;
    errors.push(`${m.text()} ${m.location().url}`);
  });
  page.on('response', (r) => { if (r.status() >= 400 && r.url() !== fixture.base + '/favicon.ico') errors.push(`HTTP ${r.status()} ${r.url()}`); });
  page.on('request', (r) => { const u = new URL(r.url()); if (/^https?:$/.test(u.protocol) && u.origin !== fixture.base) errors.push(`external ${u}`); });
  return async (label) => {
    assert.deepEqual(errors, [], label);
    const toast = await page.locator('#toast').textContent();
    assert.ok(!/JS ERROR|PROMISE REJECT|ReferenceError|TypeError/.test(toast || ''), `${label}: ${toast}`);
  };
}
async function enter(page, mobile) {
  const view = await page.locator('body').getAttribute('data-app-view');
  if (view === 'super-agent') return;
  if (mobile) await page.locator(view === 'studio' ? '#studioOpenSidebar' : '#openSidebar').click();
  await page.locator('#openSuperAgent').click();
  await page.locator('#superAgentView').waitFor({ state: 'visible' });
}
async function select(page, tid) {
  await page.locator('#superAgentRefresh').click();
  await page.locator(`[data-super-thread="${tid}"]`).click();
  await page.waitForFunction((id) => document.querySelector(`[data-super-thread="${id}"].active`), tid);
}
async function layout(page, mobile, check) {
  await check('layout');
  const info = await page.evaluate(() => {
    const rect = (s) => document.querySelector(s).getBoundingClientRect().toJSON();
    return { overflow: document.documentElement.scrollWidth > innerWidth + 1,
      sidebar: getComputedStyle(document.querySelector('#sidebar')).display,
      left: rect('.super-agent-thread-panel'), middle: rect('.super-agent-detail-panel'), right: rect('.super-agent-side-panel'),
      nav: rect('#superAgentAppNav'), width: innerWidth };
  });
  assert.equal(info.sidebar, 'none');
  assert.equal(info.overflow, false, JSON.stringify(info));
  assert.ok(info.nav.width > 0 && info.nav.right <= info.width + 1);
  if (!mobile) assert.ok(info.left.right <= info.middle.left + 1 && info.middle.right <= info.right.left + 1, JSON.stringify(info));
}
async function cardReplay(page, check) {
  // Execute production renderer with replay events; actual approvals below use HTTP.
  const result = await page.evaluate(async () => {
    const { handleChatEvent } = await import('/js/12-chat-input.js');
    const { state } = await import('/js/01-core.js');
    const row = document.createElement('div');
    row.dataset.sawTool = 'true';
    row.innerHTML = '<div class="answer-content"></div>';
    document.body.append(row);
    const event = { type: 'tool_confirm', confirm_id: 'renderer-replay', tool_name: 'list_directory', tool_desc: 'test', arguments: {}, run_id: 'renderer' };
    handleChatEvent({ ...event, confirm_processing: true }, row, state.conversationId, 'renderer');
    const first = { buttons: row.querySelectorAll('button').length, text: row.textContent };
    handleChatEvent({ ...event, confirm_resolved: true }, row, state.conversationId, 'renderer');
    const repeat = { cards: row.querySelectorAll('.tool-confirm').length, buttons: row.querySelectorAll('button').length, text: row.textContent };
    row.remove();
    return { first, repeat };
  });
  assert.equal(result.first.buttons, 0); assert.match(result.first.text, /正在执行/);
  assert.equal(result.repeat.cards, 1); assert.equal(result.repeat.buttons, 0); assert.match(result.repeat.text, /确认已结束/);
  await check('first processing and duplicate resolved renderer');
}
async function approvalCase(page, mobile, decision, check, name) {
  const created = await control({ operation: 'feedback_chat', title: `${name} · ${decision === 'allow' ? '允许' : '拒绝'}确认` });
  const tid = created.thread_id;
  const detail = () => api(`/api/super-agent/threads/${tid}`);
  await until(async () => (await detail()).thread.chat_confirmations?.some((c) => c.status === 'pending'), 'pending live confirmation');
  await until(async () => (await api(`/api/super-agent/threads/${tid}/nodes`)).nodes[0].status === 'waiting_user', 'waiting node projection');
  await enter(page, mobile); await select(page, tid);
  const item = page.locator('#superAgentChatConfirmationList [data-confirm-status="pending"]');
  await item.waitFor();
  assert.equal(await page.locator('#superAgentNodeList > [data-node-id]').count(), 1);
  assert.equal(await page.locator('#superAgentStageChain > [data-node-id]').count(), 0);
  assert.equal(await page.locator('#superAgentStageChain #superAgentNodeList').count(), 1);
  await layout(page, mobile, check);
  await page.screenshot({ path: path.join(runRoot, `${name}-waiting.png`) });
  await item.locator('[data-super-conversation]').click();
  await page.waitForFunction(() => document.body.dataset.appView === 'chat');
  const card = page.locator('.tool-confirm').last();
  await card.locator('.tool-confirm-approve').waitFor();
  await check('back to original chat approval');
  if (decision === 'allow') {
    await card.locator('.tool-confirm-approve').click();
    await card.locator('.tool-confirm-status').filter({ hasText: '已允许，工具正在执行' }).waitFor();
    assert.equal(await card.locator('button').count(), 0);
    await enter(page, mobile); await select(page, tid);
    await page.locator('#superAgentChatConfirmationList [data-confirm-status="processing"]').waitFor();
    await check('processing projection');
    // Reload while held: first replay must not resurrect approval buttons.
    await page.locator('#superAgentChatConfirmationList [data-super-conversation]').click();
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('.tool-confirm .tool-confirm-status').filter({ hasText: '正在执行' }).waitFor();
    assert.equal(await page.locator('.tool-confirm-approve').count(), 0);
    await check('processing replay after reload');
    await control({ operation: 'release_feedback', thread_id: tid });
  } else {
    await card.locator('.tool-confirm-reject').click();
    await check('rejected via original chat interface');
  }
  await until(async () => (await detail()).thread.chat_confirmations.length === 0, 'projection cleared');
  await until(async () => (await api(`/api/super-agent/threads/${tid}/nodes`)).nodes[0].status === 'succeeded', 'original chat run settled');
  await enter(page, mobile); await select(page, tid);
  assert.equal(await page.locator('#superAgentChatConfirmationList [data-confirm-id]').count(), 0);
  await check('confirmation resolved and task settled');
  console.log(`PASS ${name} ${decision}: real chat / ToolExecutor / original approval API / no duplicate buttons`);
}
async function viewportCase(viewport) {
  const mobile = viewport.width === 375;
  const context = await browser.newContext({ viewport, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const check = guard(page);
  const name = mobile ? 'mobile' : 'desktop';
  try {
    await page.goto(fixture.base, { waitUntil: 'networkidle' });
    if (await page.locator('#onboardingDialog[open]').count()) await page.locator('#onboardingSkip').click();
    assert.equal(await page.title(), 'Cat Chat');
    await enter(page, mobile); await layout(page, mobile, check);
    await page.locator('#superAgentCreate').click();
    await page.locator('#superAgentCreateDomain').selectOption('chat');
    await page.locator('#superAgentCreateConversation option[value]:not([value=""])').first().waitFor({ state: 'attached' });
    assert.equal(await page.locator('#superAgentCreateInstruction').count(), 0);
    assert.equal(await page.locator('#superAgentCreateModelOptions').getAttribute('open'), null);
    assert.equal(await page.locator('#superAgentCreateMixedOptions').getAttribute('open'), null);
    await page.locator('#superAgentCreateGoal').fill('整理这次任务的计划');
    await check('natural form');
    await page.screenshot({ path: path.join(runRoot, `${name}-create.png`) });
    await page.locator('#superAgentCreateClose').click();
    await page.locator('#superAgentAppNav [data-super-view="chat"]').click();
    await page.locator('#messageInput').waitFor({ state: 'visible' });
    await check('return chat navigation');
    if (!mobile) {
      await page.locator('#collapseSidebar').click();
      await page.evaluate(async () => (await import('/js/28-super-agent.js')).openSuperAgentView());
      await layout(page, mobile, check);
      await page.locator('#superAgentAppNav [data-super-view="chat"]').click();
      assert.equal(await page.evaluate(() => localStorage.getItem('naibaChatSidebarCollapsed')), '1');
      assert.ok(await page.locator('.app-shell').evaluate((e) => e.classList.contains('sidebar-collapsed')));
      await page.evaluate(async () => (await import('/js/14-file-panel.js')).setLeftSidebarCollapsed(false));
    }
    await cardReplay(page, check);
    await approvalCase(page, mobile, 'allow', check, name);
    await approvalCase(page, mobile, 'reject', check, name);
    await page.locator('#superAgentAppNav [data-super-view="studio"]').click();
    await page.waitForFunction(() => document.body.dataset.appView === 'studio');
    await check('workbench navigation');
    console.log(`PASS ${name} layout / natural form / navigation / original sidebar preference`);
  } catch (e) {
    await page.screenshot({ path: path.join(runRoot, `${name}-failure.png`), fullPage: true }).catch(() => {});
    await check('failure diagnostics');
    throw e;
  } finally { await context.close(); }
}
async function main() {
  try {
    await start();
    await new Promise((resolve, reject) => {
      const baseline = spawn(process.execPath, [path.join(__dirname, 'browser_smoke.cjs')],
        { cwd: ROOT, env: { ...process.env, NAIBA_SMOKE_BASE: fixture.base }, stdio: 'inherit' });
      baseline.on('error', reject);
      baseline.on('exit', (code) => code === 0 ? resolve() : reject(new Error(`Base browser gate failed: ${code}`)));
    });
    browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--disable-background-networking'] });
    for (const viewport of [{ width: 1280, height: 900 }, { width: 375, height: 900 }]) await viewportCase(viewport);
    const status = await json(fixture.control_base + '/__fixture/status', undefined, { 'X-Fixture-Token': fixture.control_token });
    assert.deepEqual(status.blocked_connections, []);
    assert.equal(Object.values(status.tool_executions).reduce((a, b) => a + b, 0), 2, 'only the two allowed tools executed, each once');
    console.log(`PASS feedback gate. Local model requests: ${status.model_requests.length}. Evidence: ${runRoot}`);
  } finally { if (browser) await browser.close(); await stop(); }
}
main().catch((e) => { console.error(e.stack || e); console.error(`Evidence: ${runRoot}`); process.exitCode = 1; });
