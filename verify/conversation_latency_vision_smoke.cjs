// 由 conversation_latency_vision_smoke.py 编排，使用隔离数据库与本机模型替身。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const fixture = JSON.parse(fs.readFileSync(process.env.NAIBA_CONVERSATION_FIXTURE, 'utf8'));
const output = process.env.NAIBA_SMOKE_OUTPUT;
const results = [];
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

async function run(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: width === 375 ? 812 : 900 } });
  const ids = fixture.widths[String(width)];
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  page.on('response', response => {
    if (response.status() >= 400 && new URL(response.url()).pathname !== '/favicon.ico') {
      errors.push(`HTTP ${response.status()} ${response.url()}`);
    }
  });
  page.on('dialog', dialog => dialog.accept());
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  await page.goto(fixture.base, { waitUntil: 'networkidle' });
  await page.evaluate(async () => {
    await import(document.querySelector('script[src*="/js/main.js"]').src);
    document.querySelector('#openChatMode').click();
    window.fixtureCore = await import('/js/01-core.js');
    window.fixtureConversations = await import('/js/08-conversations.js');
  });
  const state = async () => (await page.request.get(fixture.base + '/_fixture/state')).json();
  const open = id => page.evaluate(id => window.fixtureConversations.openConversation(id), id);
  await open(ids.delete);

  // 大库中的空会话删除与同时新建都走真实服务端写入入口。
  const deletion = await page.evaluate(async id => {
    const started = performance.now();
    let deletedMs = 0;
    const removed = fetch(`/api/conversations/${id}`, { method: 'DELETE' }).then(response => {
      deletedMs = performance.now() - started;
      return response;
    });
    await new Promise(resolve => setTimeout(resolve, 120));
    const createStarted = performance.now();
    const created = await fetch('/api/conversations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: '并行新建', model_key: 'online:fixture-current', model_name: 'fixture-current' }),
    });
    const createdMs = performance.now() - createStarted;
    const deleted = await removed;
    const conversation = await created.json();
    return { deleteStatus: deleted.status, createStatus: created.status, deletedMs, createdMs,
      elapsedMs: performance.now() - started, createdId: conversation.id };
  }, ids.delete);
  assert.equal(deletion.deleteStatus, 200);
  assert.equal(deletion.createStatus, 201);
  assert.ok(deletion.deletedMs < 900, JSON.stringify(deletion));
  assert.ok(deletion.createdMs < 900, JSON.stringify(deletion));
  await page.evaluate(async () => { window.fixtureCore.state.conversationId = ''; await window.fixtureConversations.loadConversations(); });
  assert.equal((await state()).trace_count, fixture.trace_count, '保留历史有效 trace 引用');

  // 默认供应商目录尚未缓存时，新建请求必须在该目录回包前完成。
  const held = deferred();
  const received = deferred();
  const catalogDone = deferred();
  const holdCatalog = async route => {
    const key = route.request().postDataJSON()?.model_key;
    if (key !== 'online:fixture-default') return route.continue();
    received.resolve();
    await held.promise;
    await route.continue();
    catalogDone.resolve();
  };
  await page.route('**/api/providers/models', holdCatalog);
  await page.evaluate(async () => {
    const models = await import('/js/07-models-agents.js');
    models.invalidateModelCatalogs(['online:fixture-default']);
  });
  if (width === 375) await page.click('#openSidebar');
  const createResponse = page.waitForResponse(response => response.url().endsWith('/api/conversations') && response.request().method() === 'POST');
  const started = Date.now();
  try {
    await page.locator('[data-action="new-in-group"]').first().click();
    const createdResponse = await createResponse;
    assert.equal(createdResponse.status(), 201);
    const created = await createdResponse.json();
    assert.equal(created.model_key, 'online:fixture-default');
    assert.equal(created.model_name, 'fixture-default');
    assert.ok(Date.now() - started < 1500, '新建不等待供应商目录');
    await received.promise;
  } finally {
    held.resolve();
  }
  await catalogDone.promise;
  await page.unroute('**/api/providers/models', holdCatalog);
  await page.waitForFunction(() => document.querySelector('#modelSelect').value === 'online:fixture-default');

  async function sendImage(id, message) {
    await open(id);
    const upload = page.waitForResponse(response => response.url().endsWith('/api/uploads') && response.request().method() === 'POST');
    await page.locator('#fileInput').setInputFiles(fixture.image_path);
    assert.equal((await upload).status(), 200);
    await page.waitForFunction(() => window.fixtureCore.state.pendingFiles.some(file => file.path && !file.uploading));
    await page.fill('#messageInput', message);
    const previousAnswers = await page.locator('#messages .message-row.assistant').count();
    await page.click('#sendButton');
    await page.waitForFunction(previousAnswers => {
      const messages = document.querySelector('#messages');
      return messages.querySelectorAll('.message-row.assistant').length > previousAnswers
        && messages.textContent.includes('fixture image answer')
        && !window.fixtureCore.state.chatRunId && !window.fixtureCore.state.abortController;
    }, previousAnswers, { timeout: 25000 });
    assert.deepEqual(errors, [], '前端运行错误');
    return state();
  }
  const wireHasImage = request => request.payload.input.some(item => Array.isArray(item.content) && item.content.some(part => part.type === 'input_image'));
  const directRequests = (snapshot, model) => snapshot.requests.filter(request => !request.probe && request.model === model);

  const legacyState = await state();
  assert.equal(legacyState.conversations[ids.known].chat_supports_images, -1, '旧 false 能力通过迁移重新判定');
  let snapshot = await sendImage(ids.known, `known-${width}`);
  assert.ok(directRequests(snapshot, 'deepseek-v4.1-flash').some(wireHasImage), '主模型的 Responses 请求携带真实 input_image');
  if (width === 1280) assert.equal(snapshot.conversations[ids.known].chat_supports_images, -1, '探测暂时失败保留未知状态');

  const unknownModel = `fixture-alias-${width}`;
  snapshot = await sendImage(ids.unknown, `unknown-first-${width}`);
  assert.equal(snapshot.conversations[ids.unknown].chat_supports_images, -1);
  assert.equal(snapshot.probes[unknownModel], 1, '未知别名首轮探测失败');
  assert.ok(!wireHasImage(directRequests(snapshot, unknownModel).at(-1)));
  // 再次实际上传图片，未确认能力允许重新探测。
  snapshot = await sendImage(ids.unknown, `unknown-retry-${width}`);
  assert.equal(snapshot.probes[unknownModel], 2);
  assert.equal(snapshot.conversations[ids.unknown].chat_supports_images, 1);
  assert.ok(wireHasImage(directRequests(snapshot, unknownModel).at(-1)), '重探成功后附件直接发给主模型');
  snapshot = await sendImage(ids.explicit, `explicit-${width}`);
  assert.ok(wireHasImage(directRequests(snapshot, 'fixture-explicit').at(-1)));
  assert.equal(snapshot.probes['fixture-explicit'] || 0, 0, '显式图片能力直接生效');
  snapshot = await sendImage(ids.text, `text-${width}`);
  assert.ok(!wireHasImage(directRequests(snapshot, 'fixture-text').at(-1)));
  assert.equal(snapshot.probes['fixture-text'] || 0, 0);
  assert.equal(snapshot.conversations[ids.text].model_key, 'online:fixture-text');

  // 编辑重发在真实截断回执等待期间保留输入能力，并阻止第二次提交。
  const editRequests = [];
  const chatRequests = [];
  page.on('request', request => {
    const pathname = new URL(request.url()).pathname;
    if (pathname === '/api/messages/edit') editRequests.push(request.postDataJSON());
    if (pathname === '/api/chat') chatRequests.push(request.postDataJSON());
  });
  async function beginEdit(id, text) {
    await open(id);
    await page.locator('#messages .message-row.user[data-message-id] [data-edit-message]').first().click();
    await page.fill('#messageInput', text);
  }
  async function heldEdit() {
    const entered = deferred();
    const released = deferred();
    const handler = async route => { entered.resolve(); await released.promise; await route.continue(); };
    await page.route('**/api/messages/edit', handler);
    return { entered, released, handler };
  }
  await beginEdit(ids.explicit, `edited-success-${width}`);
  await page.evaluate(() => {
    window.fixtureCore.state.contextAtCeiling = true;
  });
  let editHold = await heldEdit();
  await page.click('#sendButton');
  await editHold.entered.promise;
  assert.equal(await page.locator('#messageInput').isEnabled(), true, '旧历史已满时等待编辑仍可输入');
  assert.equal(await page.locator('#sendButton').isDisabled(), true);
  assert.equal(await page.locator('#sendButton').getAttribute('title'), '正在应用编辑');
  await page.press('#messageInput', 'Enter');
  await page.evaluate(() => document.querySelector('#composerForm').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  assert.equal(editRequests.length, 1, '等待期间仅提交一次截断');
  assert.equal(chatRequests.length, 0, '等待截断时不会提前发送新请求');
  await page.screenshot({ path: path.join(output, `edit-pending-${width}.png`) });
  editHold.released.resolve();
  await page.waitForFunction(() => !window.fixtureCore.state.messageEditPendingIds.size && !window.fixtureCore.state.chatBusy
    && document.querySelector('#messages').textContent.includes('fixture image answer'));
  await page.unroute('**/api/messages/edit', editHold.handler);
  assert.equal(chatRequests.length, 1);
  assert.equal(chatRequests[0].display_message, `edited-success-${width}`);
  assert.equal(await page.locator('#messageInput').isEnabled(), true);
  await page.fill('#messageInput', '编辑回复完成后仍可继续输入');
  await page.fill('#messageInput', '');

  await beginEdit(ids.explicit, `edited-draft-${width}`);
  editHold = await heldEdit();
  await page.click('#sendButton');
  await editHold.entered.promise;
  await page.fill('#messageInput', `next-unsent-${width}`);
  await page.press('#messageInput', 'Enter');
  editHold.released.resolve();
  await page.waitForFunction(() => !window.fixtureCore.state.messageEditPendingIds.size);
  await page.unroute('**/api/messages/edit', editHold.handler);
  assert.equal(chatRequests.length, 1, '等待期间新草稿保留，自动重发暂停');
  const draft = await page.inputValue('#messageInput');
  assert.ok(draft.includes(`next-unsent-${width}`) && draft.includes(`edited-draft-${width}`));
  assert.match(await page.textContent('#toast'), /历史已截到编辑位置.*尚未发送/);
  assert.equal(await page.locator('#sendButton').isEnabled(), true);

  // 请求失败后同时还原进入编辑前的草稿、附件与本次编辑内容。
  await page.fill('#messageInput', '');
  await open(ids.text);
  await page.fill('#messageInput', `draft-before-edit-${width}`);
  const failedUpload = page.waitForResponse(response => response.url().endsWith('/api/uploads') && response.request().method() === 'POST');
  await page.locator('#fileInput').setInputFiles(fixture.image_path);
  assert.equal((await failedUpload).status(), 200);
  await page.waitForFunction(() => window.fixtureCore.state.pendingFiles.some(file => file.path && !file.uploading));
  await page.locator('#messages .message-row.user[data-message-id] [data-edit-message]').first().click();
  await page.fill('#messageInput', `failed-edit-${width}`);
  const abortEdit = route => route.abort('failed');
  await page.route('**/api/messages/edit', abortEdit);
  await page.click('#sendButton');
  await page.waitForFunction(() => !window.fixtureCore.state.messageEditPendingIds.size
    && document.querySelector('#messageInput').value.includes('failed-edit-'));
  await page.unroute('**/api/messages/edit', abortEdit);
  const restored = await page.inputValue('#messageInput');
  assert.ok(restored.includes(`draft-before-edit-${width}`) && restored.includes(`failed-edit-${width}`));
  assert.ok(await page.evaluate(() => window.fixtureCore.state.pendingFiles.length >= 1));
  assert.equal(await page.locator('#messageInput').isEnabled(), true);
  assert.equal(await page.locator('#sendButton').isEnabled(), true);
  assert.equal(chatRequests.length, 1);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(output, `conversation-${width}.png`), fullPage: true });
  const result = { width, deletion, knownDirectImages: directRequests(snapshot, 'deepseek-v4.1-flash').filter(wireHasImage).length,
    unknownProbes: snapshot.probes[unknownModel], editRequests: editRequests.length, editChatSends: chatRequests.length, errors };
  results.push(result);
  console.log(JSON.stringify(result));
  await page.close();
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const width of [1280, 375]) await run(browser, width);
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(results, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
