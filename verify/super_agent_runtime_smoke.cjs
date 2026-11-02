// Feedback gate: isolated real App/HTTP, loopback model, real ToolExecutor.
// No production port, external models, mocked application routes or DB edits.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const ROOT = path.resolve(__dirname, '..');
const runRoot = fs.mkdtempSync(path.join(__dirname, '_super-runtime-'));
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
  const expected = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    if (m.location().url === fixture.base + '/favicon.ico' && /^Failed to load resource:.*404/.test(m.text())) return;
    if (expected.some((item) => m.location().url === item.url && m.text().includes(String(item.status)))) return;
    errors.push(`${m.text()} ${m.location().url}`);
  });
  page.on('response', (r) => {
    const known = expected.find((item) => r.url() === item.url && r.status() === item.status && !item.seen);
    if (known) { known.seen = true; return; }
    if (r.status() >= 400 && r.url() !== fixture.base + '/favicon.ico') errors.push(`HTTP ${r.status()} ${r.url()}`);
  });
  page.on('request', (r) => { const u = new URL(r.url()); if (/^https?:$/.test(u.protocol) && u.origin !== fixture.base) errors.push(`external ${u}`); });
  const check = async (label) => {
    assert.deepEqual(errors, [], label);
    const toast = await page.locator('#toast').textContent();
    assert.ok(!/JS ERROR|PROMISE REJECT|ReferenceError|TypeError/.test(toast || ''), `${label}: ${toast}`);
  };
  check.expectHTTP = (endpoint, status) => { const item = { url: fixture.base + endpoint, status, seen: false }; expected.push(item); return item; };
  return check;
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
  await page.waitForFunction((id) => document.querySelector('#superAgentDetail').dataset.threadId === id && !document.querySelector('#superAgentDetail').hidden, tid);
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

async function refresh(page, tid, check) {
  await select(page, tid);
  await check('refresh ' + tid);
}
async function createViaForm(page, setup, supervised, check, mobile, options = {}) {
  await enter(page, mobile);
  await page.locator('#superAgentCreate').click();
  await page.locator('#superAgentCreateDomain').selectOption(supervised ? 'chat' : options.domain || 'subagent');
  await page.locator(`#superAgentCreateConversation option[value="${setup.conversation_id}"]`).waitFor({ state: 'attached' });
  await page.locator('#superAgentCreateConversation').selectOption(setup.conversation_id);
  await page.locator('#superAgentCreateGoal').fill(setup.marker + ' 写完整流畅文案');
  await page.locator('#superAgentCreateName').fill(setup.marker);
  if (supervised) {
    await page.locator('#superAgentCreateExecutionMode').selectOption('supervised');
    await page.locator('#superAgentCreateCriteria').fill('[主观]文案流畅');
  } else if (!options.domain || options.domain === 'subagent') assert.match(await page.locator('#superAgentCreateRuntimeHint').textContent(), /不会自动读取原会话聊天记录/);
  await page.locator('#superAgentCreateAutostart').uncheck();
  const response = page.waitForResponse((r) => r.url() === fixture.base + '/api/super-agent/threads' && r.request().method() === 'POST');
  await page.locator('#superAgentCreateSubmit').click();
  const created = await (await response).json();
  assert.ok(created.thread, JSON.stringify(created));
  const tid = created.thread.thread_id;
  await page.locator('#superAgentCreateDialog').waitFor({ state: 'hidden' });
  await page.waitForFunction((id) => document.querySelector('#superAgentDetail').dataset.threadId === id && !document.querySelector('#superAgentDetail').hidden, tid);
  await check('created through actual UI');
  const before = await json(fixture.control_base + '/__fixture/status', undefined, { 'X-Fixture-Token': fixture.control_token });
  assert.equal(before.scenarios[setup.marker].requests, options.initialRequests || 0, 'create-only never dispatches');
  if (options.start === false) return tid;
  await page.locator('[data-super-action="start"]').click();
  await check('manual start');
  return tid;
}
async function nodeState(tid, expected) {
  return until(async () => {
    const rows = (await api(`/api/super-agent/threads/${tid}/nodes`)).nodes;
    return rows.every((node) => node.status === expected) && rows;
  }, 'all nodes ' + expected, 35000);
}
async function resultCheck(page, setup, check, expectedAttempts = 1) {
  await page.locator('[data-super-result]').first().click();
  await check('opened result detail');
  await page.locator('#superAgentResultBody').getByText('RESULT_BODY_' + setup.marker, { exact: false }).first().waitFor();
  assert.match(await page.locator('#superAgentResultBody').textContent(), /唯一正文/);
  assert.equal(await page.locator('#superAgentResultBody .super-agent-result').count(), expectedAttempts, 'current and earlier attempts are separately available');
  await check('on-demand result from real HTTP');
  await page.locator('#superAgentResultClose').click();
}
async function supervisedCase(page, kind, check, mobile) {
  const setup = await control({ operation: 'runtime_setup', kind });
  const tid = await createViaForm(page, setup, true, check, mobile);
  if (kind === 'supervised_needs_user') {
    await until(async () => (await api(`/api/super-agent/threads/${tid}`)).thread.supervision?.phase === 'waiting_user', 'needs user');
    await refresh(page, tid, check);
    await page.locator('#superAgentSupervision').getByText('需要你处理：', { exact: false }).waitFor();
    await page.reload({ waitUntil: 'networkidle' });
    await enter(page, mobile); await refresh(page, tid, check);
    await page.locator('[data-super-accept-completion]').click();
    await check('explicit user completion');
  }
  await nodeState(tid, 'succeeded');
  await refresh(page, tid, check);
  const state = (await api(`/api/super-agent/threads/${tid}`)).thread.supervision;
  assert.equal(state.round, kind === 'supervised_continue' ? 2 : 1);
  assert.ok(state.token_usage?.total_tokens > 0, JSON.stringify(state.token_usage));
  assert.equal(await page.locator('.super-agent-review-round').count(), state.round);
  if (kind === 'supervised_continue') {
    await page.locator('#superAgentConversationLinks [data-super-conversation]').click();
    await page.waitForFunction(() => document.body.dataset.appView === 'chat');
    await page.locator(`[data-supervised-thread="${tid}"]`).last().waitFor();
    assert.equal(await page.locator(`[data-supervised-thread="${tid}"]`).count(), 2);
    await check('trusted automatic source labels in original conversation');
    await enter(page, mobile); await refresh(page, tid, check);
  }
  await page.screenshot({ path: path.join(runRoot, `${mobile ? 'mobile' : 'desktop'}-${kind}.png`) });
  await check(kind);
  console.log('PASS', mobile ? 'mobile' : 'desktop', kind, 'round', state.round, 'usage', JSON.stringify(state.token_usage));
}
async function supervisedControlCase(page, check, mobile, cancel) {
  const setup = await control({ operation: 'runtime_setup', kind: cancel ? 'supervised_cancel' : 'supervised_control' });
  const tid = await createViaForm(page, setup, true, check, mobile);
  await until(async () => {
    const status = await json(fixture.control_base + '/__fixture/status', undefined, { 'X-Fixture-Token': fixture.control_token });
    return status.scenarios[setup.marker].executions === 1;
  }, 'held first execution');
  await refresh(page, tid, check);
  await page.locator(`[data-super-thread-action="${cancel ? 'cancel' : 'pause'}"]`).click();
  if (!cancel) {
    await until(async () => (await api(`/api/super-agent/threads/${tid}`)).thread.supervision.phase === 'paused', 'durable pause');
    await control({ operation: 'runtime_release', marker: setup.marker });
    await until(async () => {
      const detail = await api(`/api/conversations/${setup.conversation_id}`);
      return detail?.messages?.some((m) => m.role === 'assistant');
    }, 'in-flight execution settled while paused');
    const paused = (await api(`/api/super-agent/threads/${tid}`)).thread.supervision;
    assert.equal(paused.phase, 'paused'); assert.equal(paused.review_count, 0);
    await page.reload({ waitUntil: 'networkidle' }); await enter(page, mobile); await refresh(page, tid, check);
    await page.locator('[data-super-thread-action="continue"]').click();
    await nodeState(tid, 'succeeded');
  } else {
    await control({ operation: 'runtime_release', marker: setup.marker });
    await nodeState(tid, 'cancelled');
  }
  await refresh(page, tid, check);
  await check(cancel ? 'supervised cancel' : 'supervised pause / refresh / explicit continue');
  console.log('PASS', mobile ? 'mobile' : 'desktop', cancel ? 'supervised cancel' : 'supervised pause/continue');
}
async function subagentCase(page, decision, check, mobile) {
  const setup = await control({ operation: 'runtime_setup', kind: decision === 'text' ? 'subagent_text' : 'subagent_approval' });
  const tid = await createViaForm(page, setup, false, check, mobile);
  if (decision !== 'text') {
    await until(async () => (await api(`/api/super-agent/threads/${tid}`)).thread.chat_confirmations?.some((c) => c.status === 'pending'), 'subagent pending');
    await refresh(page, tid, check);
    const card = page.locator('#superAgentChatConfirmationList [data-confirm-status="pending"]');
    await card.waitFor();
    assert.equal(await card.locator('[data-super-conversation]').count(), 0);
    await card.locator(`[data-super-tool-decision="${decision === 'reject' ? 'reject' : 'confirm'}"]`).click();
    await check('subagent approval via original API');
    if (decision === 'allow') {
      await until(async () => (await api(`/api/super-agent/threads/${tid}`)).thread.chat_confirmations?.some((c) => c.status === 'processing'), 'subagent processing');
      await page.reload({ waitUntil: 'networkidle' }); await enter(page, mobile); await refresh(page, tid, check);
      assert.equal(await page.locator('[data-super-tool-decision]').count(), 0);
      await page.locator('[data-confirm-status="processing"]').waitFor();
      await check('processing refresh has no approval buttons');
      await control({ operation: 'runtime_release', marker: setup.marker });
    }
  }
  await nodeState(tid, decision === 'reject' ? 'failed' : 'succeeded');
  await refresh(page, tid, check);
  assert.equal(await page.locator('[data-super-action="resume"]').count(), 0);
  if (decision === 'reject') {
    await page.locator('[data-super-result]').first().click();
    await page.locator('#superAgentResultBody').getByText('未执行', { exact: false }).first().waitFor();
    assert.match(await page.locator('#superAgentResultBody').textContent(), /拒绝/);
    await check('refused action exposes real non-success reason');
    await page.locator('#superAgentResultClose').click();
    await page.locator('[data-super-action="retry"]').click();
    await until(async () => (await api(`/api/super-agent/threads/${tid}`)).thread.chat_confirmations?.some((c) => c.status === 'pending'), 'retry pending');
    await refresh(page, tid, check);
    await page.locator('[data-super-tool-decision="confirm"]').click();
    await control({ operation: 'runtime_release', marker: setup.marker });
    await nodeState(tid, 'succeeded'); await refresh(page, tid, check);
    assert.equal((await api(`/api/super-agent/threads/${tid}/nodes`)).nodes[0].attempt, 1);
    await check('explicit retry after refused action');
  }
  await resultCheck(page, setup, check, decision === 'reject' ? 2 : 1);
  await page.screenshot({ path: path.join(runRoot, `${mobile ? 'mobile' : 'desktop'}-subagent-${decision}.png`) });
  console.log('PASS', mobile ? 'mobile' : 'desktop', 'subagent', decision);
}
async function chainCase(page, check, mobile, nextDomain) {
  const setup = await control({ operation: 'runtime_setup', kind: 'subagent_text' });
  const selection = { provider_id: 'probe', model_key: 'online:probe', model_name: 'probe', conversation_id: setup.conversation_id };
  const created = await api('/api/super-agent/threads', { domain: 'mixed', title: setup.marker, autostart: true, auto_advance: true,
    nodes: [{ key: 'one', domain: 'subagent', ...selection, start_request: { instruction: setup.marker + ' 生成唯一正文' } },
      { key: 'two', domain: nextDomain, ...selection, start_request: nextDomain === 'chat' ? { message: '根据前序结果整理' } : { instruction: '根据前序结果整理' } }] });
  const tid = created.thread.thread_id;
  await nodeState(tid, 'succeeded');
  await enter(page, mobile); await refresh(page, tid, check);
  const status = await json(fixture.control_base + '/__fixture/status', undefined, { 'X-Fixture-Token': fixture.control_token });
  const requests = status.model_requests.filter((r) => JSON.stringify(r.messages).includes('RESULT_BODY_' + setup.marker));
  assert.ok(requests.length, 'actual successor request includes predecessor body');
  const successor = requests.at(-1);
  assert.ok(!(successor.tools || []).some((t) => t.function?.name === 'job_output'));
  await resultCheck(page, setup, check);
  console.log('PASS', mobile ? 'mobile' : 'desktop', 'subagent ->', nextDomain, 'body transfer');
}
async function cancelCase(page, check, mobile) {
  const setup = await control({ operation: 'runtime_setup', kind: 'subagent_approval' });
  const tid = await createViaForm(page, setup, false, check, mobile);
  await until(async () => (await api(`/api/super-agent/threads/${tid}`)).thread.chat_confirmations?.length, 'cancel pending');
  await refresh(page, tid, check);
  await page.locator('[data-super-action="cancel"]').click();
  await nodeState(tid, 'cancelled'); await refresh(page, tid, check);
  assert.equal(await page.locator('[data-super-tool-decision]').count(), 0);
  await check('cancel pending clears approval');
}
async function expiredCase(page, check, mobile) {
  const setup = await control({ operation: 'runtime_setup', kind: 'subagent_approval' });
  const tid = await createViaForm(page, setup, false, check, mobile);
  const pending = await until(async () => (await api(`/api/super-agent/threads/${tid}`)).thread.chat_confirmations?.find((c) => c.status === 'pending'), 'stale approval pending');
  await refresh(page, tid, check);
  await page.waitForLoadState('networkidle');
  let releaseSnapshots;
  const snapshotsHeld = new Promise((resolve) => { releaseSnapshots = resolve; });
  const holdSnapshots = async (route) => { if (route.request().method() === 'GET') await snapshotsHeld; await route.continue(); };
  // Hold only delivery timing of refresh GETs. Every request still reaches the
  // real server unchanged; the stale approval itself uses the real tool route.
  await page.route('**/api/super-agent/threads/**', holdSnapshots);
  try {
  // Another client resolves the real pending confirmation; this page still has
  // its prior snapshot. The production route must reject the stale approval.
  await api('/api/tool/reject', { run_id: pending.run_id, confirm_id: pending.confirm_id });
  const expected = check.expectHTTP('/api/tool/confirm', 409);
  const clicked = await page.locator('[data-super-tool-decision="confirm"]').evaluate((button) => { const info = { disabled: button.disabled, connected: button.isConnected, runId: button.dataset.runId }; button.dispatchEvent(new MouseEvent('click', { bubbles: true })); return info; });
  assert.equal(clicked.disabled, false, JSON.stringify(clicked));
  await check('stale confirmation clicked');
  await page.locator('#superAgentChatConfirmationList').getByText('确认已失效或未受理：', { exact: false }).waitFor();
  assert.ok(expected.seen, 'exact stale approval conflict exercised');
  assert.equal(await page.locator('[data-super-tool-decision="confirm"]:enabled').count(), 0);
  await check('stale confirmation has explicit failure and no active button');
  } finally { releaseSnapshots(); await page.unroute('**/api/super-agent/threads/**', holdSnapshots); }
  await nodeState(tid, 'failed');
  await refresh(page, tid, check);
  console.log('PASS', mobile ? 'mobile' : 'desktop', 'stale confirmation rejected by real HTTP');
}
async function draftCase(page, check, mobile) {
  await enter(page, mobile); await page.locator('#superAgentCreate').click();
  await page.locator('#superAgentCreateDomain').selectOption('writer');
  await page.locator(`#superAgentCreateBookId option[value="${fixture.writer_book_id}"]`).waitFor({ state: 'attached' });
  await page.locator('#superAgentCreateBookId:enabled').selectOption(fixture.writer_book_id);
  await page.locator('#superAgentCreateBookHint').filter({ hasText: /章/ }).waitFor();
  await page.locator('#superAgentCreateMode').selectOption('single');
  await page.locator('#superAgentCreateStage').selectOption('split');
  await page.locator('#superAgentCreateFrom').fill('2'); await page.locator('#superAgentCreateCount').fill('2');
  await page.locator('#superAgentCreatePart').selectOption('1');
  await page.locator('#superAgentCreateMixedOptions').evaluate((element) => { element.open = true; });
  await page.locator('#superAgentCreateAutostart').check();
  await page.locator('#superAgentCreateAddStep').click();
  assert.equal(await page.locator('#superAgentCreateSteps li').count(), 1);
  assert.match(await page.locator('#superAgentCreateSteps').textContent(), /拆章.*第 2–3 章/);
  await page.locator('#superAgentCreateStage').selectOption('chapter');
  await page.locator('#superAgentCreateChapter').fill('2'); await page.locator('#superAgentCreateAddStep').click();
  assert.equal(await page.locator('#superAgentCreateSteps li').count(), 2);
  assert.match(await page.locator('#superAgentCreateDraftStatus').textContent(), /等待前序拆章生成/);
  await page.locator('[data-super-remove-step="0"]').click();
  await page.locator('#superAgentCreateMixedSubmit').click();
  await page.locator('#superAgentCreateError').filter({ hasText: '依赖前序拆章' }).waitFor();
  await check('planned chapter accepts same-book predecessor and revalidates removal');
  await page.locator('[data-super-remove-step="0"]').click();
  await page.locator('#superAgentCreateStage').selectOption('split');
  await page.locator('#superAgentCreateAddStep').click();
  await page.locator('#superAgentCreateDomain').selectOption('chat');
  await page.locator(`#superAgentCreateConversation option[value="${fixture.conversation_id}"]`).waitFor({ state: 'attached' });
  await page.locator('#superAgentCreateConversation').selectOption(fixture.conversation_id);
  await page.locator('#superAgentCreateExecutionMode').selectOption('supervised');
  assert.equal(await page.locator('#superAgentCreateSteps li:visible').count(), 1);
  assert.equal(await page.locator('#superAgentCreateAddStep').isDisabled(), true);
  assert.match(await page.locator('#superAgentCreateRuntimeHint').textContent(), /已保留 1 个步骤/);
  await page.locator('#superAgentCreateExecutionMode').selectOption('single');
  await page.locator('#superAgentCreateGoal').fill('混合草稿只创建，不执行');
  await page.locator('#superAgentCreateAddStep').click();
  await page.locator('#superAgentCreateDomain').selectOption('job');
  await page.locator(`#superAgentCreateConversation option[value="${fixture.conversation_id}"]`).waitFor({ state: 'attached' });
  await page.locator('#superAgentCreateConversation').selectOption(fixture.conversation_id);
  await page.locator('#superAgentCreateJob').selectOption(fixture.job_id);
  assert.equal(await page.locator('#superAgentCreateAutostart').isChecked(), true);
  assert.equal(await page.locator('#superAgentCreateAutostart').isEnabled(), true);
  await page.locator('#superAgentCreateAddStep').click();
  await page.locator('#superAgentCreateAutostart').uncheck();
  const response = page.waitForResponse((r) => r.url() === fixture.base + '/api/super-agent/threads' && r.request().method() === 'POST');
  await page.locator('#superAgentCreateMixedSubmit').click();
  const result = await (await response).json();
  assert.equal(result.nodes.length, 3);
  assert.deepEqual(result.nodes.map((node) => node.executor_type), ['writer_pipeline', 'chat_run', 'job']);
  assert.ok(result.nodes.every((node) => !node.executor_id));
  await page.locator('#superAgentCreateDialog').waitFor({ state: 'hidden' });
  await check('mixed draft modes and Job autostart state preserved through real creation');
  console.log('PASS', mobile ? 'mobile' : 'desktop', 'mixed draft modes/planned chapter/Job autostart');
}
async function busyCase(page, check, mobile) {
  const setup = await control({ operation: 'runtime_setup', kind: 'chat_busy' });
  const busy = await control({ operation: 'runtime_busy', marker: setup.marker });
  await until(async () => (await json(fixture.control_base + '/__fixture/status', undefined, { 'X-Fixture-Token': fixture.control_token })).scenarios[setup.marker].requests === 1, 'original busy request held');
  const tid = await createViaForm(page, setup, false, check, mobile, { domain: 'chat', start: false, initialRequests: 1 });
  const endpoint = `/api/super-agent/threads/${tid}/actions`;
  const expected = check.expectHTTP(endpoint, 400);
  const rejectedResponse = page.waitForResponse((r) => r.url() === fixture.base + endpoint && r.request().method() === 'POST');
  await page.locator('[data-super-action="start"]').click();
  const rejected = await rejectedResponse;
  const rejection = await rejected.json();
  assert.equal(rejection.submission, 'not_submitted', JSON.stringify(rejection));
  assert.ok(expected.seen);
  const originalRequest = rejected.request().postDataJSON();
  await page.locator('#superAgentSyncStatus').getByText('操作失败：', { exact: false }).waitFor();
  await check('busy original run rejected without dispatch');
  assert.equal((await api(`/api/runs/${busy.run_id}`)).status, 'running');
  await control({ operation: 'runtime_release', marker: setup.marker });
  await until(async () => (await api(`/api/runs/${busy.run_id}`)).status === 'completed', 'original run settled');
  await refresh(page, tid, check);
  const secondResponse = page.waitForResponse((r) => r.url() === fixture.base + endpoint && r.request().method() === 'POST');
  await page.locator('[data-super-action="start"]').click();
  const second = await secondResponse;
  assert.equal(second.status(), 200);
  assert.notEqual(second.request().postDataJSON().request_id, originalRequest.request_id, 'explicit retry of known not-submitted start uses fresh id');
  await nodeState(tid, 'succeeded'); await refresh(page, tid, check);
  await resultCheck(page, setup, check);
  console.log('PASS', mobile ? 'mobile' : 'desktop', 'busy original run ends / fresh explicit start succeeds');
}
async function main() {
  try {
    await start();
    browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--disable-background-networking'] });
    for (const viewport of [{ width: 1280, height: 900 }, { width: 375, height: 900 }]) {
      const mobile = viewport.width === 375;
      const context = await browser.newContext({ viewport, serviceWorkers: 'block' });
      const page = await context.newPage();
      page.setDefaultTimeout(20000); page.on('dialog', (dialog) => dialog.accept());
      const checkNoScriptError = guard(page);
      try {
        await page.goto(fixture.base, { waitUntil: 'networkidle' });
        await page.locator('#onboardingDialog[open]').waitFor({ timeout: 5000 }).then(() => page.locator('#onboardingSkip').click()).catch(() => {});
        assert.equal(await page.title(), 'Cat Chat');
        if (process.env.NAIBA_RUNTIME_CASE === 'expired') { await expiredCase(page, checkNoScriptError, mobile); continue; }
        if (process.env.NAIBA_RUNTIME_CASE === 'busy') { await busyCase(page, checkNoScriptError, mobile); continue; }
        await draftCase(page, checkNoScriptError, mobile);
        await busyCase(page, checkNoScriptError, mobile);
        if (process.env.NAIBA_RUNTIME_SCENARIO !== 'subagent') {
          for (const kind of ['supervised_done', 'supervised_continue', 'supervised_needs_user']) await supervisedCase(page, kind, checkNoScriptError, mobile);
          await supervisedControlCase(page, checkNoScriptError, mobile, false);
          await supervisedControlCase(page, checkNoScriptError, mobile, true);
        }
        if (process.env.NAIBA_RUNTIME_SCENARIO !== 'supervised') {
          for (const decision of ['text', 'allow', 'reject']) await subagentCase(page, decision, checkNoScriptError, mobile);
          await chainCase(page, checkNoScriptError, mobile, 'subagent');
          await chainCase(page, checkNoScriptError, mobile, 'chat');
          await cancelCase(page, checkNoScriptError, mobile);
          await expiredCase(page, checkNoScriptError, mobile);
        }
        await layout(page, mobile, checkNoScriptError);
      } catch (error) { await page.screenshot({ path: path.join(runRoot, `${mobile ? 'mobile' : 'desktop'}-failure.png`), fullPage: true }); await checkNoScriptError('failure diagnostics'); throw error; }
      finally { await context.close(); }
    }
    const status = await json(fixture.control_base + '/__fixture/status', undefined, { 'X-Fixture-Token': fixture.control_token });
    assert.deepEqual(status.blocked_connections, []);
    console.log(`PASS runtime gate; ${status.model_requests.length} loopback model HTTP calls. Evidence: ${runRoot}`);
  } finally { if (browser) await browser.close(); await stop(); }
}
main().catch((e) => { console.error(e.stack || e); console.error(`Evidence: ${runRoot}`); process.exitCode = 1; });
