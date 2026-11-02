// V1 reusable Edge/Playwright gate. Run only when coordinator authorizes browser use.
// node verify/super_agent_v1_ui_smoke.cjs (project NODE_PATH; project venv Python).
// Optional NAIBA_V1_UI_ROOT (NEW/EMPTY), NAIBA_V1_UI_PORT (0=ephemeral),
// NAIBA_V1_UI_FIXTURE (existing service fixture; do not spawn), NAIBA_V1_UI_SHOTS.
// Real app/HTTP throughout; no page.route, no mocked /threads creation, no external model.
// Chat/subagent are CREATE-ONLY autostart=false, NOT runtime execution passes.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const ownedRoot = process.env.NAIBA_V1_UI_ROOT || fs.mkdtempSync(path.join(os.tmpdir(), 'naiba-v1-ui-'));
const fixturePath = process.env.NAIBA_V1_UI_FIXTURE || path.join(ownedRoot, 'fixture.json');
const SHOTS = process.env.NAIBA_V1_UI_SHOTS || path.join(ownedRoot, 'screenshots');
let fixture;
let server;
let output = '';
const failures = [];
const check = (label, ok, detail = '') => {
  assert.ok(ok, `${label}${detail ? `: ${detail}` : ''}`);
  console.log(`PASS ${label}`);
};
async function json(method, url, body, headers = {}, expected = 200) {
  const response = await fetch(url, { method, headers: { 'Content-Type': 'application/json', ...headers }, body: body === undefined ? undefined : JSON.stringify(body) });
  const payload = await response.json();
  assert.equal(response.status, expected, `${method} ${url}: ${JSON.stringify(payload)}`);
  return payload;
}
const api = (method, suffix, body, expected = 200) => json(method, fixture.base + suffix, body, {}, expected);
const control = (body) => json('POST', fixture.control_base + '/__fixture/control', body, { 'X-Fixture-Token': fixture.control_token });
const fixtureStatus = () => json('GET', fixture.control_base + '/__fixture/status', undefined, { 'X-Fixture-Token': fixture.control_token });
async function startFixture() {
  if (!process.env.NAIBA_V1_UI_FIXTURE) {
    server = spawn(path.join(ROOT, '.venv', 'Scripts', 'python.exe'), [path.join(__dirname, '_serve_super_agent_v1_ui.py'), '--root', ownedRoot, '--port', process.env.NAIBA_V1_UI_PORT || '0'], {
      cwd: ROOT, env: { ...process.env, PYTHONIOENCODING: 'utf-8', NO_PROXY: 'localhost,127.0.0.1,::1' }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    server.stdout.on('data', (chunk) => { output += chunk.toString(); });
    server.stderr.on('data', (chunk) => { output += chunk.toString(); });
    server.on('error', (error) => { output += error.stack; });
  }
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    if (server && server.exitCode !== null) throw new Error(`Fixture exited ${server.exitCode}\n${output}`);
    if (fs.existsSync(fixturePath)) {
      fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
      assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(fixture.base).hostname));
      try { const response = await fetch(fixture.base); if (response.ok) return; } catch {}
    }
    await sleep(100);
  }
  throw new Error(`Fixture startup timeout\n${output}`);
}
async function stopFixture() {
  if (!server) return;
  fs.writeFileSync(path.join(ownedRoot, 'server.log'), output, 'utf8');
  if (server.exitCode === null) {
    const ended = new Promise((resolve) => server.once('exit', resolve));
    server.kill();
    await Promise.race([ended, sleep(5000)]);
  }
}
function monitor(page) {
  const errors = [];
  const bad = [];
  const external = [];
  const expected = [];
  // Exact request URL/method/status, consumed once. No broad 4xx/5xx allowlist.
  const expectHTTP = (suffix, status) => { const item = { url: fixture.base + suffix, status, seen: false }; expected.push(item); return item; };
  page.on('pageerror', (error) => errors.push(`pageerror ${error.message}`));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    // Chromium emits a resource diagnostic for deliberately tested real HTTP 400s.
    // Never exempt console.error from app JS, and never exempt an unrelated URL.
    const location = message.location();
    if (location.url === fixture.base + '/favicon.ico' && /^Failed to load resource: the server responded with a status of 404\b/.test(message.text())) return;
    const networkDiagnostic = /^Failed to load resource: the server responded with a status of (400|409)\b/.test(message.text());
    if (networkDiagnostic && expected.some((item) => item.url === location.url && message.text().includes(String(item.status)))) return;
    errors.push(`console.error ${message.text()} ${location.url || ''}`);
  });
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (/^https?:$/.test(url.protocol) && url.origin !== new URL(fixture.base).origin) external.push(request.url());
  });
  page.on('response', (response) => {
    if (response.status() < 400) return;
    if (response.status() === 404 && response.url() === fixture.base + '/favicon.ico') return;
    const item = expected.find((entry) => !entry.seen && response.request().method() === 'POST' && entry.url === response.url() && entry.status === response.status());
    if (item) item.seen = true; else bad.push(`${response.status()} ${response.url()}`);
  });
  const checkNoScriptError = async (label) => {
    assert.deepEqual(errors, [], `${label}: script errors`);
    assert.deepEqual(bad, [], `${label}: unexpected HTTP errors`);
    assert.deepEqual(external, [], `${label}: external request forbidden`);
    const bar = await page.locator('#toast').textContent().catch(() => '');
    assert.ok(!/JS错误|PROMISE|ReferenceError|SyntaxError/.test(bar || ''), `${label}: ${bar}`);
  };
  return { checkNoScriptError, expectHTTP, expected };
}
async function step(page, guard, label, work) {
  await guard.checkNoScriptError(`before ${label}`);
  try { const result = await work(); await guard.checkNoScriptError(label); console.log(`PASS ${label}`); return result; }
  catch (error) { await guard.checkNoScriptError(`failed ${label}`); throw error; }
}
const field = (page, suffix) => page.locator(`#superAgentCreate${suffix}`);
async function settled(page, domain) {
  const resource = field(page, ['writer', 'novel'].includes(domain) ? 'BookId' : 'Conversation');
  await resource.locator('option:not([value=""])').first().waitFor({ state: 'attached' });
  await page.waitForFunction((id) => { const el = document.getElementById(id); return el && !el.disabled && el.value; }, await resource.getAttribute('id'));
  if (['writer', 'novel'].includes(domain)) {
    await page.waitForFunction(() => /《/.test(document.getElementById('superAgentCreateBookHint').textContent));
  }
}
async function openCreate(page, guard) {
  await step(page, guard, 'open create', async () => {
    await page.locator('#superAgentCreate').click();
    await field(page, 'Dialog').waitFor({ state: 'visible' });
    await settled(page, await field(page, 'Domain').inputValue());
    await page.waitForFunction(() => document.getElementById('superAgentCreateModel').value === 'probe');
  });
}
async function chooseDomain(page, guard, domain, mode = 'chain', retry = false) {
  await step(page, guard, `choose ${domain} ${mode}`, async () => {
    await field(page, 'Domain').selectOption(domain);
    await settled(page, domain);
    if (['writer', 'novel'].includes(domain)) {
      // Select by human-readable title; never fill an opaque book ID.
      const title = domain === 'writer' ? (retry ? fixture.retry_writer_title : fixture.writer_title) : fixture.novel_title;
      await field(page, 'BookId').selectOption({ label: title });
      await page.waitForFunction((title) => document.getElementById('superAgentCreateBookHint').textContent.includes(title), title);
      await field(page, 'Mode').selectOption(mode);
      if (domain === 'writer' && mode === 'chain') {
        await field(page, 'Parts').fill('第一部：起点\n第二部：转折');
        await field(page, 'Part').selectOption('1');
        await field(page, 'From').fill('2');
        await field(page, 'Count').fill('2');
        await field(page, 'Chapter').fill('2');
      } else if (domain === 'novel') {
        await field(page, 'ChapterStart').selectOption('1');
        await field(page, 'ChapterEnd').selectOption('2');
      }
    } else {
      await field(page, 'Conversation').selectOption({ label: fixture.conversation_title });
      if (domain === 'job') await field(page, 'Job').selectOption(fixture.job_id);
      else await field(page, 'Goal').fill(`V1 ${domain} create-only; no execution`);
    }
    await field(page, 'Autostart').uncheck();
  });
}
async function submit(page, guard, label, button = 'Submit') {
  return step(page, guard, label, async () => {
    const responsePromise = page.waitForResponse((response) => response.request().method() === 'POST' && response.url() === fixture.base + '/api/super-agent/threads');
    await field(page, button).click();
    const response = await responsePromise;
    const body = response.request().postDataJSON();
    const payload = await response.json();
    check(`${label}: real HTTP 201`, response.status() === 201, JSON.stringify(payload));
    check(`${label}: DTO no checkpoint/raw`, payload.nodes.every((node) => !('checkpoint' in node) && !('raw' in node)));
    check(`${label}: stable request identity`, !!body.request_id && body.request_id === body.idempotency_key);
    await field(page, 'Dialog').waitFor({ state: 'hidden' });
    await page.waitForFunction((id) => document.querySelector(`[data-super-thread="${id}"]`)?.classList.contains('active'), payload.thread.thread_id);
    return { body, ...payload };
  });
}
async function layout(page, guard, name) {
  await step(page, guard, `${name}: layout`, async () => {
    const info = await page.evaluate(() => {
      const dialog = document.getElementById('superAgentCreateDialog');
      const rect = dialog.getBoundingClientRect();
      const form = document.getElementById('superAgentCreateForm');
      const title = document.getElementById('superAgentCreateTitle').getBoundingClientRect();
      const close = document.getElementById('superAgentCreateClose').getBoundingClientRect();
      const actions = form.querySelector('.writer-dialog-actions').getBoundingClientRect();
      return { left: rect.left, right: rect.right, width: innerWidth, height: rect.height, overflow: dialog.scrollWidth > dialog.clientWidth + 1,
        docOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
        formOverflowY: getComputedStyle(form).overflowY, titleOverlap: title.right > close.left, actionsInside: actions.left >= rect.left - 1 && actions.right <= rect.right + 1 };
    });
    check(`${name}: no horizontal overflow/title overlap`, info.left >= -1 && info.right <= info.width + 1 && !info.overflow && !info.docOverflow && !info.titleOverlap, JSON.stringify(info));
    check(`${name}: scrolling form, contained buttons`, info.formOverflowY !== 'visible' && info.actionsInside);
    if (name === 'mobile') check('mobile: full height dialog', Math.abs(info.height - 900) <= 2);
    await page.screenshot({ path: path.join(SHOTS, `${name}-create.png`) });
  });
}
async function validation(page, guard, expectedText) {
  const before = (await api('GET', '/api/super-agent/threads')).threads.length;
  await step(page, guard, `validation ${expectedText}`, async () => {
    await field(page, 'Submit').click();
    await field(page, 'Error').filter({ hasText: expectedText }).waitFor();
    check('invalid form stays open', await field(page, 'Dialog').isVisible());
    check('invalid form creates nothing', (await api('GET', '/api/super-agent/threads')).threads.length === before);
  });
}
async function nodeAction(page, guard, threadId, nodeId, action, expectedStatus = 200) {
  return step(page, guard, `node ${action}`, async () => {
    if (action === 'retry' || action === 'cancel') page.once('dialog', (dialog) => dialog.accept());
    const endpoint = `/api/super-agent/threads/${threadId}/actions`;
    const expected = expectedStatus >= 400 ? guard.expectHTTP(endpoint, expectedStatus) : null;
    const [response] = await Promise.all([
      page.waitForResponse((response) => response.url() === fixture.base + endpoint && response.request().method() === 'POST'),
      page.locator(`#superAgentStageChain [data-super-node="${nodeId}"][data-super-action="${action}"]`).click(),
    ]);
    assert.equal(response.status(), expectedStatus);
    if (expected) assert.ok(expected.seen);
    const result = await response.json();
    if (response.ok()) await page.waitForFunction(() => !document.getElementById('superAgentSyncStatus').textContent.includes('正在'));
    return { payload: result, body: response.request().postDataJSON() };
  });
}
async function selectThread(page, guard, id) {
  await step(page, guard, 'select existing real thread', async () => {
    await page.locator('#superAgentRefresh').click();
    await page.locator(`[data-super-thread="${id}"]`).click();
    const expected = (await api('GET', `/api/super-agent/threads/${id}/nodes`)).nodes;
    await page.waitForFunction((ids) => ids.every((id) => document.querySelector(`#superAgentStageChain [data-node-id="${id}"]`)), expected.map((row) => row.node_id));
  });
}
async function syncUntil(page, guard, threadId, nodeId, status) {
  for (let i = 0; i < 100; i += 1) {
    let nodes = (await api('GET', `/api/super-agent/threads/${threadId}/nodes`)).nodes;
    if (nodes.find((row) => row.node_id === nodeId).status === status) {
      await page.locator('#superAgentRefresh').click();
      await page.waitForFunction(({ id, status }) => document.querySelector(`#superAgentStageChain [data-node-id="${id}"]`)?.classList.contains(`status-${status}`), { id: nodeId, status });
      await guard.checkNoScriptError(`settled ${status}`);
      return nodes;
    }
    // The real reconciler can finish between HTTP observation and a click, removing
    // its Sync button. Wait for durable state instead of racing a vanished control.
    await sleep(100);
  }
  throw new Error(`node ${nodeId} did not become ${status}`);
}
async function approval(page, guard, threadId, approvalId, status, expectedStatus = 200) {
  return step(page, guard, `approval ${status} HTTP ${expectedStatus}`, async () => {
    const endpoint = `/api/super-agent/threads/${threadId}/approvals/${approvalId}`;
    if (expectedStatus >= 400) guard.expectHTTP(endpoint, expectedStatus);
    const promise = page.waitForResponse((response) => response.url() === fixture.base + endpoint && response.request().method() === 'POST');
    await page.locator(`#superAgentApprovalList [data-super-approval-id="${approvalId}"][data-super-approval="${status}"]`).click();
    const response = await promise;
    assert.equal(response.status(), expectedStatus, JSON.stringify(await response.json()));
    if (expectedStatus >= 400) await page.locator('#superAgentSyncStatus').filter({ hasText: '不属于' }).waitFor();
    else await page.waitForFunction(() => !document.getElementById('superAgentSyncStatus').textContent.includes('正在'));
  });
}
async function browserCase(browser, viewport) {
  await control({ operation: 'reset_failures' });
  const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, serviceWorkers: 'block', offline: false });
  const page = await context.newPage();
  const guard = monitor(page);
  try {
    await step(page, guard, `${viewport.name}: boot`, async () => {
      await page.goto(fixture.base, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(1000);
      if (await page.locator('#onboardingDialog[open]').count()) await page.locator('#onboardingSkip').click();
      assert.ok((await page.title()).includes('Cat Chat'));
      if (viewport.name === 'mobile') await page.locator('#openSidebar').click();
      await page.locator('#openSuperAgent').click();
      await page.locator('#superAgentView').waitFor({ state: 'visible' });
    });
    await openCreate(page, guard);
    await layout(page, guard, viewport.name);
    await step(page, guard, 'global default and provider catalogs', async () => {
      await field(page, 'ModelOptions').locator('summary').click();
      assert.equal(await field(page, 'Model').inputValue(), 'probe');
      assert.notEqual(await field(page, 'Model').locator('option[value]:not([value=""])').first().getAttribute('value'), 'probe');
      await field(page, 'Provider').selectOption('online:probe-alt');
      await page.waitForFunction(() => document.getElementById('superAgentCreateModel').value === 'alternate');
      assert.equal(await field(page, 'Model').locator('option[value="probe"]').count(), 0);
      await field(page, 'Provider').selectOption('online:probe');
      await page.waitForFunction(() => document.getElementById('superAgentCreateModel').value === 'probe');
    });
    await chooseDomain(page, guard, 'writer');
    await step(page, guard, 'fixed writer preview', async () => { assert.equal(await field(page, 'ChainPreview').locator('li').count(), 4); });
    await field(page, 'Count').fill('0');
    await validation(page, guard, '1–999');
    await field(page, 'Count').fill('2');
    await field(page, 'Chapter').fill('9');
    await validation(page, guard, '本次拆章范围');
    await field(page, 'Chapter').fill('2');
    await field(page, 'Parts').fill('');
    await validation(page, guard, '部标题');
    await field(page, 'Parts').fill('第一部：起点\n第二部：转折');
    await field(page, 'Part').selectOption('1');
    await step(page, guard, 'real empty model catalog', async () => {
      await control({ operation: 'catalog', mode: 'empty' });
      await field(page, 'ModelRefresh').click();
      await field(page, 'ModelHint').filter({ hasText: '暂无可用模型' }).waitFor();
    });
    await validation(page, guard, '可用模型');
    await step(page, guard, 'real catalog error and recover', async () => {
      await control({ operation: 'catalog', mode: 'error' });
      guard.expectHTTP('/api/providers/models', 400);
      await field(page, 'ModelRefresh').click();
      await field(page, 'ModelHint').filter({ hasText: '模型目录读取失败' }).waitFor();
      await control({ operation: 'catalog', mode: 'normal' });
      await field(page, 'ModelRefresh').click();
      await page.waitForFunction(() => document.getElementById('superAgentCreateModel').value === 'probe');
      assert.ok((await field(page, 'ModelHint').innerText()).includes('全局默认模型'));
    });
    const writer = await submit(page, guard, 'Writer real full V1 create');
    assert.equal(writer.body.book_id, fixture.writer_book_id);
    assert.deepEqual(writer.nodes.map((node) => node.stage_key), ['setup', 'outline', 'chapter_plan', 'chapter_content']);
    assert.equal(writer.nodes[0].status, 'ready');
    assert.ok(writer.nodes.slice(1).every((node) => node.status === 'blocked'));
    assert.deepEqual(writer.body.stage_requests.chapter_plan, { part_no: 1, from_no: 2, want_count: 2, extra_note: writer.body.goal });
    assert.equal(writer.body.stage_requests.chapter_content.chapter_no, 2);
    assert.equal(writer.body.stage_requests.outline.part_plan.length, 2);
    assert.equal(writer.body.model_name, 'probe');
    assert.equal(writer.body.provider_id, 'probe');
    assert.equal(writer.body.model_key, 'online:probe');
    assert.equal(writer.body.chain_instance_id, writer.body.request_id);
    const id = writer.thread.thread_id;
    await step(page, guard, 'pause/continue real control payloads', async () => {
      for (const action of ['pause', 'continue']) {
        const promise = page.waitForResponse((response) => response.url() === `${fixture.base}/api/super-agent/threads/${id}/actions` && response.request().method() === 'POST');
        await page.locator(`#superAgentThreadActions [data-super-thread-action="${action}"]`).click();
        const response = await promise;
        assert.equal(response.status(), 200);
        const body = response.request().postDataJSON();
        assert.equal(body.action, action); assert.equal(body.node_id, ''); assert.ok(body.request_id);
        await page.waitForFunction((status) => document.getElementById('superAgentStatus').dataset.status === status, action === 'pause' ? 'paused' : 'active');
        await guard.checkNoScriptError(`after ${action}`);
        if (action === 'pause') {
          assert.equal(await page.locator(`[data-super-node="${writer.nodes[0].node_id}"][data-super-action="start"]`).count(), 0, 'server capabilities hide start while paused');
          const rejected = await api('POST', `/api/super-agent/threads/${id}/actions`, { action: 'start', node_id: writer.nodes[0].node_id, request_id: `paused-${id}` }, 400);
          assert.ok(rejected.error.includes('暂停'));
        }
      }
    });
    await nodeAction(page, guard, id, writer.nodes[0].node_id, 'start');
    const advanced = await syncUntil(page, guard, id, writer.nodes[0].node_id, 'succeeded');
    assert.equal(advanced[1].status, 'ready');
    const events = await api('GET', `/api/super-agent/threads/${id}/events`);
    assert.equal(events.events.filter((row) => row.event_type === 'stage_promoted').length, 1);
    assert.equal((await api('GET', `/api/super-agent/threads/${id}/events?after=${events.last_sequence}`)).events.length, 0);
    await step(page, guard, 'current highlight and event cursor dedupe', async () => {
      await page.locator('#superAgentRefresh').click();
      await page.waitForFunction((id) => document.querySelector(`#superAgentStageChain [data-node-id="${id}"].is-current`), writer.nodes[1].node_id);
      const count = await page.locator('#superAgentEventList article').count();
      await page.locator('#superAgentRefresh').click();
      await page.waitForTimeout(150);
      assert.equal(await page.locator('#superAgentEventList article').count(), count);
    });
    await openCreate(page, guard);
    await chooseDomain(page, guard, 'writer', 'chain', true);
    await field(page, 'Model').selectOption('smoke-fail');
    const retry = await submit(page, guard, 'Writer retry chain');
    await nodeAction(page, guard, retry.thread.thread_id, retry.nodes[0].node_id, 'start');
    await syncUntil(page, guard, retry.thread.thread_id, retry.nodes[0].node_id, 'failed');
    const retried = await nodeAction(page, guard, retry.thread.thread_id, retry.nodes[0].node_id, 'retry');
    assert.equal(retried.payload.result.attempt, 1);
    await syncUntil(page, guard, retry.thread.thread_id, retry.nodes[0].node_id, 'succeeded');
    await openCreate(page, guard);
    await chooseDomain(page, guard, 'novel');
    assert.equal(await field(page, 'ChainPreview').locator('li').count(), 5);
    await field(page, 'ChapterStart').selectOption('3');
    await field(page, 'ChapterEnd').selectOption('1');
    await validation(page, guard, '有效起止章节');
    await field(page, 'ChapterStart').selectOption('1');
    await field(page, 'ChapterEnd').selectOption('2');
    const novel = await submit(page, guard, 'Novel real full V1 create');
    assert.deepEqual(novel.nodes.map((node) => node.stage_key), ['import', 'elements', 'outline', 'screenplay', 'review']);
    assert.deepEqual(novel.body.stage_requests.elements.chapter_ids, [1, 2]);
    assert.equal(novel.nodes[3].domain_ref.stage, 'script');
    const nid = novel.thread.thread_id;
    const imported = await nodeAction(page, guard, nid, novel.nodes[0].node_id, 'start');
    assert.equal(imported.payload.result.executor_id, '');
    await nodeAction(page, guard, nid, novel.nodes[1].node_id, 'start');
    const waiting = await syncUntil(page, guard, nid, novel.nodes[1].node_id, 'waiting_user');
    const aid = (await api('GET', `/api/super-agent/threads/${nid}/approvals`)).approvals[0].approval_id;
    await control({ operation: 'approval_task', node_id: waiting[1].node_id, broken: true });
    await approval(page, guard, nid, aid, 'approved', 400);
    await control({ operation: 'approval_task', node_id: waiting[1].node_id, broken: false });
    // Real approval failure must remain retryable; same approval/request ID, no forged DTO.
    await approval(page, guard, nid, aid, 'approved');
    const approved = (await api('GET', `/api/super-agent/threads/${nid}/nodes`)).nodes;
    assert.equal(approved[1].status, 'succeeded'); assert.equal(approved[2].status, 'ready');
    await openCreate(page, guard);
    await chooseDomain(page, guard, 'novel');
    const reject = await submit(page, guard, 'Novel rejection chain');
    await nodeAction(page, guard, reject.thread.thread_id, reject.nodes[0].node_id, 'start');
    await nodeAction(page, guard, reject.thread.thread_id, reject.nodes[1].node_id, 'start');
    await syncUntil(page, guard, reject.thread.thread_id, reject.nodes[1].node_id, 'waiting_user');
    const rejectAid = (await api('GET', `/api/super-agent/threads/${reject.thread.thread_id}/approvals`)).approvals[0].approval_id;
    await approval(page, guard, reject.thread.thread_id, rejectAid, 'rejected');
    const rejected = (await api('GET', `/api/super-agent/threads/${reject.thread.thread_id}/nodes`)).nodes;
    assert.equal(rejected[1].status, 'failed'); assert.equal(rejected[2].status, 'blocked');
    for (const domain of ['chat', 'subagent', 'job']) {
      await openCreate(page, guard);
      await chooseDomain(page, guard, domain);
      if (domain !== 'job') {
        await field(page, 'Goal').fill('');
        await validation(page, guard, '你想完成什么');
        await field(page, 'Goal').fill(`V1 ${domain} create-only; no execution`);
      }
      const created = await submit(page, guard, `${domain} create-only real HTTP`);
      assert.equal(created.body.conversation_id, fixture.conversation_id);
      assert.equal(created.body.autostart, false);
      if (domain === 'job') { assert.equal(created.body.job_id, fixture.job_id); assert.ok(!created.body.start_request); }
      else {
        assert.equal(created.body.start_request[domain === 'chat' ? 'message' : 'instruction'], `V1 ${domain} create-only; no execution`);
        assert.equal(created.body.goal, created.body.start_request[domain === 'chat' ? 'message' : 'instruction']);
        assert.ok(!created.nodes[0].executor_id, 'create-only nodes have no execution identity');
      }
    }
    await mixedCase(page, guard);
    await openCreate(page, guard);
    await layout(page, guard, viewport.name);
    await field(page, 'Close').click();
    await step(page, guard, 'final stage layout', async () => {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));
      await page.screenshot({ path: path.join(SHOTS, `${viewport.name}-mixed-chain.png`) });
    });
    await step(page, guard, 'real missing-provider bootstrap blocks create', async () => {
      await control({ operation: 'providers', empty: true });
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(800);
      if (await page.locator('#onboardingDialog[open]').count()) await page.locator('#onboardingSkip').click();
      if (viewport.name === 'mobile') await page.locator('#openSidebar').click();
      await page.locator('#openSuperAgent').click();
      await page.locator('#superAgentCreate').click();
      await settled(page, 'writer');
      await field(page, 'Submit').click();
      await field(page, 'Error').filter({ hasText: '设置' }).waitFor();
      assert.equal(await field(page, 'Provider').isEnabled(), false);
      await control({ operation: 'providers', empty: false });
    });
    await guard.checkNoScriptError('final');
    assert.ok(guard.expected.every((item) => item.seen), 'all declared HTTP failures actually exercised');
  } finally {
    await control({ operation: 'catalog', mode: 'normal' });
    await control({ operation: 'providers', empty: false });
    try { await guard.checkNoScriptError(`${viewport.name} teardown`); } finally { await context.close(); }
  }
}
async function mixedCase(page, guard) {
  await openCreate(page, guard);
  // Complete fixed chains cannot be nested. This must not append a draft step.
  await chooseDomain(page, guard, 'writer');
  await step(page, guard, 'mixed rejects nested full chain', async () => {
    await field(page, 'MixedOptions').locator('summary').click();
    await field(page, 'AddStep').click();
    await field(page, 'Error').filter({ hasText: '只做一个步骤' }).waitFor();
    assert.equal(await field(page, 'Steps').locator('li').count(), 0);
  });
  await chooseDomain(page, guard, 'chat');
  await field(page, 'AddStep').click();
  await step(page, guard, 'mixed removable draft', async () => {
    assert.equal(await field(page, 'Steps').locator('li').count(), 1);
    await field(page, 'Steps').locator('[data-super-remove-step]').click();
    assert.equal(await field(page, 'Steps').locator('li').count(), 0);
    assert.equal(await field(page, 'MixedSubmit').isEnabled(), false);
  });
  for (const domain of ['chat', 'subagent', 'writer', 'novel', 'job']) {
    await chooseDomain(page, guard, domain, 'single');
    if (domain === 'writer') { await field(page, 'Stage').selectOption('chapter'); await field(page, 'Chapter').fill('1'); }
    if (domain === 'novel') await field(page, 'Stage').selectOption('elements');
    await step(page, guard, `mixed append ${domain}`, async () => {
      await field(page, 'AddStep').click();
      assert.equal((await field(page, 'Error').innerText()).trim(), '');
    });
  }
  await step(page, guard, 'mixed payload queue visible', async () => {
    assert.equal(await field(page, 'Steps').locator('li').count(), 5);
    await field(page, 'MixedAutoAdvance').uncheck();
    await field(page, 'Autostart').uncheck();
  });
  const result = await submit(page, guard, 'heterogeneous same-task real HTTP', 'MixedSubmit');
  assert.equal(result.body.domain, 'mixed');
  assert.deepEqual(result.body.nodes.map((node) => node.domain), ['chat', 'subagent', 'writer', 'novel', 'job']);
  assert.equal(new Set(result.body.nodes.map((node) => node.key)).size, 5);
  assert.equal(result.body.auto_advance, false); assert.equal(result.body.autostart, false);
  for (const node of result.body.nodes) {
    assert.ok(!node.chain && !node.nodes && !node.request_id && !('autostart' in node) && !('auto_advance' in node));
  }
  assert.equal(result.nodes.length, 5);
  assert.equal(result.nodes[0].status, 'ready');
  assert.ok(result.nodes.slice(1).every((node) => node.status === 'blocked'));
  assert.ok(result.nodes.every((node) => !node.executor_id));
  assert.deepEqual(result.nodes.map((node) => node.executor_type), ['chat_run', 'subagent', 'writer_pipeline', 'novel_stage', 'job']);
  for (let i = 1; i < result.nodes.length; i += 1) assert.deepEqual(result.nodes[i].dependencies, [result.nodes[i - 1].node_id]);
  // Replay actual submitted body through real HTTP: no extra thread, same graph.
  const replay = await api('POST', '/api/super-agent/threads', result.body, 201);
  assert.equal(replay.thread.thread_id, result.thread.thread_id);
  assert.deepEqual(replay.nodes.map((node) => node.node_id), result.nodes.map((node) => node.node_id));
  await guard.checkNoScriptError('mixed idempotent real replay');
}
async function main() {
  await startFixture();
  fs.mkdirSync(SHOTS, { recursive: true });
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1', '--disable-background-networking'] });
    for (const viewport of [{ name: 'desktop', width: 1280, height: 900 }, { name: 'mobile', width: 375, height: 900 }]) {
      try { await browserCase(browser, viewport); }
      catch (error) { failures.push(`${viewport.name}: ${error.stack || error}`); }
    }
    const status = await fixtureStatus();
    assert.deepEqual(status.blocked_connections, [], 'fixture attempted external networking');
    assert.deepEqual(status.model_requests, [], 'create-only Chat/subagent accidentally dispatched');
    console.log('NOT COVERED: Chat/subagent model runtime execution; real external models; full final Writer/Novel chain generation.');
    console.log(`Fixture: ${fixturePath}\nScreenshots: ${SHOTS}`);
    if (failures.length) throw new Error(failures.join('\n\n'));
  } finally {
    if (browser) await browser.close();
    await stopFixture();
  }
}
if (require.main === module) main().catch(async (error) => { await stopFixture(); console.error(error.stack || error); process.exitCode = 1; });
