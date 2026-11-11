// Real source UI + isolated HTTP storage, with deterministic model catalogs and held responses.
const { chromium, request } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8827';
const output = path.join(__dirname, '_conversation_model_smoke');
fs.mkdirSync(output, { recursive: true });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const catalogs = { 'online:probe-te': ['probe-default', 'te-second'], 'online:probe-moda': ['probe-qwen', 'moda-second'] };
async function run(browser, client, viewport) {
  const clear = await client.post('/api/settings', { data: { new_conversation_model_key: '', new_conversation_model_name: '' } });
  assert.ok(clear.ok(), await clear.text());
  const create = async (title, model_key, model_name) => {
    const response = await client.post('/api/conversations', { data: { title, model_key, model_name } });
    assert.ok(response.ok()); return response.json();
  };
  const a = await create(`race-A-${viewport.width}`, 'online:probe-te', 'probe-default');
  const b = await create(`race-B-${viewport.width}`, 'online:probe-moda', 'probe-qwen');
  const branch = await create(`branch-${viewport.width}`, 'online:probe-te', 'probe-default');
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  const check = () => assert.deepEqual(errors, [], 'no pageerror/console.error after each operation');
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  await page.route('**/api/providers/models', route => {
    const key = route.request().postDataJSON().model_key;
    return route.fulfill({ json: { models: (key ? catalogs[key] || [] : ['unsaved-only']).map(id => ({ id, name: id })) } });
  });
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.evaluate(async () => { await import(document.querySelector('script[src*="/js/main.js"]').src); });
  await page.evaluate(() => document.querySelector('#openChatMode').click());
  await page.evaluate(async () => {
    window.raceCore = await import('/js/01-core.js');
    window.raceConversations = await import('/js/08-conversations.js');
    window.raceModels = await import('/js/07-models-agents.js');
    window.raceMessages = await import('/js/04-messages.js');
    window.raceSettings = await import('/js/09-settings.js');
  });
  const open = id => page.evaluate(id => window.raceConversations.openConversation(id), id);
  const binding = () => page.evaluate(() => ({
    id: window.raceCore.state.conversationId,
    api: document.querySelector('#modelSelect').value,
    model: document.querySelector('#composerModelSelect').value,
  }));
  await open(a.id); check();

  // A is held after its detail request starts; B finishes first.
  const received = deferred(), release = deferred();
  const delayed = async route => { received.resolve(); await release.promise; await route.fulfill({ response: await route.fetch() }); };
  await page.route(`**/api/conversations/${a.id}`, delayed);
  await page.evaluate(id => { window.pendingOpen = window.raceConversations.openConversation(id); }, a.id);
  await received.promise; await open(b.id); release.resolve();
  assert.equal(await page.evaluate(() => window.pendingOpen), false);
  assert.deepEqual(await binding(), { id: b.id, api: b.model_key, model: b.model_name }); check();
  await page.unroute(`**/api/conversations/${a.id}`, delayed);

  // Reverse only first_turn completion order; the view stays coherent.
  const firstReceived = deferred(), firstRelease = deferred();
  const firstDelayed = async route => { firstReceived.resolve(); await firstRelease.promise; await route.fulfill({ json: { system: 'A first turn' } }); };
  await page.route(`**/api/conversations/${a.id}/first_turn`, firstDelayed);
  await page.evaluate(id => { window.pendingOpen = window.raceConversations.openConversation(id); }, a.id);
  await firstReceived.promise; assert.equal((await binding()).id, b.id);
  await open(b.id); firstRelease.resolve(); await page.evaluate(() => window.pendingOpen);
  assert.equal((await binding()).id, b.id); check();
  await page.unroute(`**/api/conversations/${a.id}/first_turn`, firstDelayed);

  // A starts a subscription while the request for B is held; committing B detaches A.
  await open(a.id);
  const runReceived = deferred(), runRelease = deferred();
  const runDelayed = async route => { runReceived.resolve(); await runRelease.promise; await route.fulfill({ response: await route.fetch() }); };
  await page.route(`**/api/conversations/${b.id}`, runDelayed);
  await page.evaluate(id => { window.pendingOpen = window.raceConversations.openConversation(id); }, b.id);
  await runReceived.promise;
  await page.evaluate(id => {
    window.oldRunController = new AbortController();
    window.raceCore.state.abortController = window.oldRunController;
    window.raceCore.state.runConversationId = id;
  }, a.id);
  runRelease.resolve(); assert.equal(await page.evaluate(() => window.pendingOpen), true);
  assert.deepEqual(await page.evaluate(() => ({
    aborted: window.oldRunController.signal.aborted,
    runConversationId: window.raceCore.state.runConversationId,
    connected: Boolean(window.raceCore.state.abortController),
  })), { aborted: true, runConversationId: '', connected: false });
  assert.equal((await binding()).id, b.id); check();
  await page.unroute(`**/api/conversations/${b.id}`, runDelayed);

  // A's API save arrives after opening B; only A's HTTP record/cache change.
  await open(a.id);
  const saveReceived = deferred(), saveRelease = deferred();
  const saveDelayed = async route => { saveReceived.resolve(); await saveRelease.promise; await route.fulfill({ response: await route.fetch() }); };
  await page.route(`**/api/conversations/${a.id}/settings`, saveDelayed);
  await page.selectOption('#modelSelect', 'online:probe-moda', { force: true });
  await saveReceived.promise; await open(b.id); saveRelease.resolve();
  await page.evaluate(async id => { const pending = window.raceModels.pendingConversationModelSave(id); if (pending) await pending; }, a.id);
  assert.deepEqual(await binding(), { id: b.id, api: b.model_key, model: b.model_name });
  assert.equal((await (await client.get(`/api/conversations/${a.id}`)).json()).model_key, 'online:probe-moda');
  assert.equal((await (await client.get(`/api/conversations/${b.id}`)).json()).model_name, 'probe-qwen'); check();
  await page.unroute(`**/api/conversations/${a.id}/settings`, saveDelayed);

  // Editing is truncated server-side before reload. Navigating away prevents all automatic sends.
  await open(a.id);
  const editReceived = deferred(), editRelease = deferred(); let sends = 0;
  await page.route('**/api/chat', route => { sends++; return route.abort(); });
  await page.route('**/api/messages/edit', async route => { editReceived.resolve(); await editRelease.promise; await route.fulfill({ json: { attachments: [] } }); });
  await page.evaluate(() => { window.pendingEdit = window.raceMessages.resendFromUserMessage('fixture-message', '人工编辑内容保留'); });
  await editReceived.promise; await open(b.id); editRelease.resolve();
  assert.equal(await page.evaluate(() => window.pendingEdit), false); assert.equal(sends, 0);
  assert.match(await page.inputValue('#messageInput'), /人工编辑内容保留/); assert.equal((await binding()).id, b.id); check();

  // A late branch creates a list entry without pre-filling B.
  await open(a.id); await page.fill('#messageInput', 'B 应保留的输入');
  const branchReceived = deferred(), branchRelease = deferred();
  await page.route(`**/api/conversations/${a.id}/branch`, async route => { branchReceived.resolve(); await branchRelease.promise; await route.fulfill({ json: { conversation: branch, branch_message: { content: '不能覆盖 B' } } }); });
  await page.evaluate(() => { window.pendingBranch = window.raceMessages.branchMessage({ dataset: { messageId: 'fixture-message' } }); });
  await page.click('#branchAgentKeep'); await branchReceived.promise; await open(b.id); branchRelease.resolve();
  await page.evaluate(() => window.pendingBranch);
  assert.equal(await page.inputValue('#messageInput'), 'B 应保留的输入'); assert.equal((await binding()).id, b.id); check();

  // Settings UI saves a complete independent pair. Default applies to new conversations only.
  await page.evaluate(() => document.querySelector('#settingsDialog').showModal());
  await page.click('[data-settings-tab="models"]');
  await page.selectOption('#newConversationDefaultApi', 'online:probe-te');
  await page.waitForFunction(() => [...document.querySelector('#newConversationDefaultModel').options].some(o => o.value === 'te-second'));
  await page.selectOption('#newConversationDefaultModel', 'te-second');
  await page.click('#newConversationDefaultSave');
  await page.waitForFunction(() => window.raceCore.state.bootstrap.settings.new_conversation_model_name === 'te-second');
  assert.equal((await binding()).id, b.id); assert.equal((await binding()).model, 'probe-qwen');
  const fieldsFit = await page.locator('.new-conversation-default-fields').evaluate(el => [...el.querySelectorAll('select')].every(s => s.getBoundingClientRect().right <= innerWidth + 1));
  assert.ok(fieldsFit); check();
  await page.screenshot({ path: path.join(output, `defaults-${viewport.width}.png`) });
  // Checking an unsaved connection can update its form, but cannot replace the saved API catalog.
  const savedCatalog = await page.evaluate(() => window.raceCore.state.providerModelCatalogs['online:probe-te']);
  await page.evaluate(() => window.raceSettings.openProviderCard('probe-te'));
  await page.fill('#providerBaseUrl', 'https://unsaved.example.invalid/v1');
  await page.click('#loadProviderModels');
  await page.waitForFunction(() => [...document.querySelector('#providerModel').options].some(o => o.value === 'unsaved-only'));
  assert.deepEqual(await page.evaluate(() => window.raceCore.state.providerModelCatalogs['online:probe-te']), savedCatalog);
  assert.equal((await binding()).model, 'probe-qwen'); check();
  await page.evaluate(() => window.raceSettings.cancelProviderEdit());
  await page.evaluate(() => document.querySelector('#settingsDialog').close());
  assert.equal(await page.evaluate(() => window.raceConversations.createConversation()), true);
  const created = await binding(); assert.equal(created.api, 'online:probe-te'); assert.equal(created.model, 'te-second');
  const stored = await (await client.get(`/api/conversations/${created.id}`)).json(); assert.equal(stored.model_name, 'te-second');
  check();

  // Known invalid catalog => preserve the current composer, without changing the saved default.
  await open(b.id);
  await page.evaluate(() => {
    window.raceCore.state.providerModelCatalogs['online:probe-te'] = [{ id: 'probe-default' }];
    window.raceSettings.renderNewConversationDefault({ reset: true });
  });
  assert.match(await page.textContent('#newConversationDefaultStatus'), /失效/);
  await page.evaluate(() => window.raceConversations.createConversation());
  assert.equal((await binding()).model, 'probe-qwen'); check();
  await page.evaluate(() => window.raceSettings.saveNewConversationDefault(true));
  await open(b.id); await page.evaluate(() => window.raceConversations.createConversation());
  assert.equal((await binding()).model, 'probe-qwen'); check();

  // Full reload reads the HTTP binding, not the previous conversation's controls.
  await page.reload({ waitUntil: 'networkidle' });
  await page.evaluate(async id => { const c = await import('/js/08-conversations.js'); await c.openConversation(id); }, b.id);
  assert.equal(await page.inputValue('#modelSelect'), 'online:probe-moda');
  assert.equal(await page.inputValue('#composerModelSelect'), 'probe-qwen'); check();
  console.log(`conversation/model smoke ${viewport.width}: navigation, model queue, edit/branch, defaults, reload PASS`);
  await page.close();
}
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const client = await request.newContext({ baseURL: BASE });
  try { for (const viewport of [{ width: 1280, height: 900 }, { width: 375, height: 812 }]) await run(browser, client, viewport); }
  finally { await client.dispose(); await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
