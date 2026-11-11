// API 供应商卡片化冒烟（源码 server，端口 8790）。
// 覆盖：卡片网格一行最多三张 / 末尾固定「添加 API」卡 / 点卡片弹出设置并加载预设 /
//       Esc 与右上角关闭复位 / × 删除走确认 / 添加卡片新建 / 在线-本地 tab 过滤 / 窄屏 2 列与 1 列。
// 复制专项：桌面与 375px 真保存验证预填、空 Key、取消、重名递增和独立编辑；只跑专项设 NAIBA_PROVIDER_COPY_ONLY=1。
// 前置：源码 server 已在 8790 运行（config.json 已备份，脚本自行播种并在收尾清理）。
// 运行：$env:NODE_PATH="<node_modules 目录>"; node verify\provider_cards_smoke.cjs
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const PREFIX = '冒烟供应商';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(path, options = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, ...options };
  // fetch 不会自动序列化对象：直接传对象会发成 "[object Object]"。
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(`${BASE}${path}`, init);
  return response.json().catch(() => ({}));
}

function seeded(name) {
  return String(name || '').startsWith(PREFIX);
}

async function cleanupSeeded() {
  const bootstrap = await apiJson('/api/bootstrap');
  const list = bootstrap.model_profiles || bootstrap.providers || [];
  for (const provider of list) {
    if (!seeded(provider.name)) continue;
    await apiJson(`/api/providers/${encodeURIComponent(provider.id)}`, { method: 'DELETE' });
  }
}

async function cardSnapshot(page) {
  return page.evaluate(() => {
    const container = document.querySelector('#providerCards');
    if (!container) return null;
    const cards = [...container.querySelectorAll('.provider-card')];
    const add = container.querySelector('[data-provider-add]');
    const style = getComputedStyle(container);
    return {
      total: cards.length,
      columns: style.gridTemplateColumns.split(' ').filter(Boolean).length,
      addIsLast: Boolean(add) && container.lastElementChild === add,
      addText: add ? add.textContent.trim() : '',
      providers: cards.filter((card) => card.dataset.providerCard).map((card) => ({
        id: card.dataset.providerCard,
        name: card.querySelector('.provider-card-name')?.textContent.trim() || '',
        // 卡片已压成两行（名称 + 脚行），不再展示模型名：模型名改由设置弹层核对。
        hasModelText: Boolean(card.querySelector('.provider-card-model')),
        tag: card.querySelector('.provider-card-tag')?.textContent.trim() || '',
        badge: card.querySelector('.provider-card-badge')?.textContent.trim() || '',
        hasDelete: Boolean(card.querySelector('[data-provider-delete]')),
        cursor: getComputedStyle(card).cursor,
        minHeight: getComputedStyle(card).minHeight,
      })),
    };
  });
}

async function dialogSnapshot(page) {
  return page.evaluate(() => {
    const dialog = document.querySelector('#providerDialog');
    const model = document.querySelector('#providerModel');
    return {
      open: Boolean(dialog && dialog.open),
      title: document.querySelector('#providerDialogTitle')?.textContent.trim() || '',
      subtitle: document.querySelector('#providerDialogSubtitle')?.textContent.trim() || '',
      name: document.querySelector('#providerName')?.value || '',
      baseUrl: document.querySelector('#providerBaseUrl')?.value || '',
      model: model ? model.value : '',
      nameDisabled: document.querySelector('#providerName')?.disabled,
      formatDisabled: document.querySelector('#providerFormat')?.disabled,
      hasSave: Boolean(document.querySelector('#saveProvider')),
      editButtonGone: !document.querySelector('#editProvider'),
    };
  });
}

async function waitForClosed(page, timeout = 20000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const open = await page.evaluate(() => Boolean(document.querySelector('#providerDialog')?.open));
    if (!open) return true;
    await page.waitForTimeout(200);
  }
  return false;
}

async function waitForCardCount(page, expected, timeout = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const snapshot = await cardSnapshot(page);
    if (snapshot && snapshot.providers.length === expected) return snapshot;
    await page.waitForTimeout(200);
  }
  return cardSnapshot(page);
}

async function runProviderCopyViewport(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  const shots = process.env.NAIBA_SMOKE_SHOTS || path.join(__dirname, '_tmp_provider_copy_20261111');
  fs.mkdirSync(shots, { recursive: true });
  const errors = [], saves = [], secretRequests = [], createdIds = [];
  const sourceName = `复制验收${width}-${Date.now()}供应商`;
  let modelGate = null;
  let releaseModelGate = null;
  let secretGate = null;
  let releaseSecretGate = null;
  let expectedSecretRequests = 0;
  const sourceValues = {
    name: sourceName, kind: 'online', base_url: 'https://provider-copy.invalid/v1',
    request_format: 'claude', model: 'copy-fixture-model', context_window: 65536,
    max_output_tokens: 2345, temperature: 0.7, reasoning_effort: 'high', supports_images: false,
    api_key: `fixture-source-${width}`,
  };
  const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  const profiles = async () => (await apiJson('/api/bootstrap')).model_profiles || [];
  const seed = async values => {
    const saved = await apiJson('/api/providers', { method: 'POST', body: values });
    assert.ok(saved.id, '隔离服务创建测试供应商');
    createdIds.push(saved.id);
    return saved;
  };
  const noErrors = async () => {
    assert.deepEqual(errors, [], '无脚本或非预期接口错误');
    assert.equal(await page.locator('body').evaluate(el => /JS ERROR|PROMISE REJECT/.test(el.innerText)), false);
    assert.equal(secretRequests.length, expectedSecretRequests, '普通复制与编辑流程不请求已保存的 API Key');
  };
  const openSettings = async () => {
    if (width <= 760) {
      const sidebarOpen = await page.locator('#sidebar').evaluate(el => el.classList.contains('open'));
      if (!sidebarOpen) await page.locator('#openSidebar').click();
    }
    await page.locator('#openSettings').click();
    await page.locator('#settingsDialog[open]').waitFor();
    await page.locator('.settings-nav button[data-settings-tab="models"]').click();
    await page.locator('[data-provider-kind="online"]').click();
  };
  const form = () => page.evaluate(() => {
    const value = selector => document.querySelector(selector).value;
    const model = value('#providerModel');
    return {
      id: value('#providerId'), name: value('#providerName'), baseUrl: value('#providerBaseUrl'),
      format: value('#providerFormat'), kind: value('#providerKind'),
      model: model === '__custom__' ? value('#providerModelCustom') : model,
      context: value('#providerContextWindow'), output: value('#providerMaxOutputTokens'),
      temperature: value('#providerTemperature'), reasoning: value('#providerReasoningEffort'),
      images: value('#providerSupportsImages'), keyEmpty: value('#providerApiKey') === '',
      keyStatus: document.querySelector('#providerKeyStatus').textContent,
      keyType: document.querySelector('#providerApiKey').type, focus: document.activeElement?.id,
    };
  });
  const closeDraft = async () => {
    await page.locator('#cancelProvider').click();
    await page.locator('#providerDialog').waitFor({ state: 'hidden' });
  };
  const saveDraft = async () => {
    const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/providers'
      && response.request().method() === 'POST' && response.status() === 200);
    await page.locator('#saveProvider').click();
    const payload = await (await saved).json();
    await page.locator('#providerDialog').waitFor({ state: 'hidden' });
    return payload;
  };
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('response', response => {
    if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) errors.push(`${response.status()} ${new URL(response.url()).pathname}`);
  });
  page.on('request', request => {
    const pathname = new URL(request.url()).pathname;
    if (pathname.endsWith('/secret')) secretRequests.push(pathname);
    if (pathname === '/api/providers' && request.method() === 'POST') {
      const values = request.postDataJSON();
      // 只记录身份和是否填写密钥；测试日志和断言不保留密钥内容。
      saves.push({ id: values.id, name: values.name, hasKey: Boolean(values.api_key) });
    }
  });
  await page.addInitScript(() => { localStorage.clear(); localStorage.setItem('naibaOnboardingDismissed', '1'); });
  // 只隔离模型目录，不拦截供应商创建/编辑/删除和 bootstrap 持久读回。
  await page.route('**/api/providers/models', async route => {
    if (modelGate) {
      const pending = modelGate;
      modelGate = null;
      pending.arrived();
      await pending.wait;
    }
    return json(route, { models: [{
      id: sourceValues.model, name: sourceValues.model, context_window: 65536, max_output_tokens: 2345,
    }] });
  });
  await page.route('**/api/providers/*/secret', async route => {
    if (secretGate) {
      const pending = secretGate;
      secretGate = null;
      pending.arrived();
      await pending.wait;
    }
    return json(route, { api_key: `fixture-source-${width}` });
  });
  try {
    const source = await seed(sourceValues);
    const sourceBefore = (await profiles()).find(item => item.id === source.id);
    assert.equal(sourceBefore.has_api_key, true, '源卡带有独立测试密钥');
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await openSettings();
    const cards = page.locator('#providerCards [data-provider-card]');
    assert.equal(await page.locator('#providerCards [data-provider-copy]').count(), await cards.count(), '每张在线供应商卡都有复制按钮');
    const copy = page.locator(`[data-provider-copy="${source.id}"]`);
    await copy.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(shots, `provider-copy-${width}-cards.png`) });
    await copy.click();
    await page.locator('#providerDialog[open]').waitFor();
    assert.deepEqual(await form(), {
      id: '', name: `${sourceName}1`, baseUrl: sourceValues.base_url, format: sourceValues.request_format,
      kind: '0', model: sourceValues.model, context: '65536', output: '2345', temperature: '0.7',
      reasoning: 'high', images: 'false', keyEmpty: true, keyStatus: '未配置', keyType: 'password', focus: 'providerApiKey',
    }, '复制为独立新草稿，预填配置、清空密钥并聚焦 Key');
    await page.screenshot({ path: path.join(shots, `provider-copy-${width}-form.png`) });
    await page.locator('#toggleProviderKey').click();
    assert.equal((await form()).keyEmpty, true, '点击显示仍为空，不读取源密钥');
    await noErrors();
    const beforeCancel = (await profiles()).length;
    await closeDraft();
    assert.equal(saves.length, 0, '取消复制不发送保存请求');
    assert.equal((await profiles()).length, beforeCancel, '取消复制不创建记录');
    assert.deepEqual((await profiles()).find(item => item.id === source.id), sourceBefore, '取消不改变源卡');

    // 保存前的可选模型探测尚未结束时取消，迟到探测不能继续创建新卡。
    await copy.click();
    await page.locator('#providerDialog[open]').waitFor();
    await page.locator('#providerContextWindow').fill('');
    await page.locator('#providerMaxOutputTokens').fill('');
    let arrived, release;
    const probeStarted = new Promise(resolve => { arrived = resolve; });
    modelGate = { arrived, wait: new Promise(resolve => { release = resolve; }) };
    releaseModelGate = release;
    await page.locator('#saveProvider').click();
    await Promise.race([probeStarted, new Promise((_, reject) => setTimeout(() => reject(new Error('保存前模型探测未发出')), 5000))]);
    await closeDraft();
    release();
    releaseModelGate = null;
    await page.waitForFunction(() => !document.querySelector('#saveProvider').disabled);
    assert.equal(saves.length, 0, '取消正在预探测的保存后，不发起创建请求');
    assert.equal((await profiles()).length, beforeCancel, '迟到预探测回包不创建记录');
    await noErrors();

    await seed({ ...sourceValues, name: `${sourceName}1`, api_key: '' });
    await seed({ ...sourceValues, name: `${sourceName}2`, api_key: '' });
    await page.reload({ waitUntil: 'networkidle' });
    await openSettings();
    // 键盘激活应进入复制草稿，不能被父卡片的 Enter 委托改成编辑原卡。
    await copy.focus();
    await page.keyboard.press('Enter');
    await page.locator('#providerDialog[open]').waitFor();
    const conflict = await form();
    assert.equal(conflict.id, '');
    assert.equal(conflict.name, `${sourceName}3`, '已有 1 和 2 时建议名称递增至 3');
    assert.equal(conflict.keyEmpty, true);
    assert.equal(conflict.focus, 'providerApiKey');
    await page.locator('#providerName').fill(`${sourceName}独立账户`);
    await page.locator('#providerApiKey').fill(`fixture-copy-${width}`);
    const copied = await saveDraft();
    assert.ok(copied.id && copied.id !== source.id, '保存返回独立新 id');
    createdIds.push(copied.id);
    assert.equal(saves.length, 1, '复制保存只发出一次现有供应商保存请求');
    assert.notEqual(saves[0].id, source.id, '复制保存身份与源供应商独立');
    assert.equal(saves[0].hasKey, true);
    const afterCopy = await profiles();
    assert.equal(afterCopy.length, beforeCancel + 3, '两个命名冲突夹具加一张真实副本');
    const storedCopy = afterCopy.find(item => item.id === copied.id);
    assert.equal(storedCopy.name, `${sourceName}独立账户`, '建议名称可手动修改');
    for (const field of ['kind', 'base_url', 'request_format', 'model', 'context_window', 'max_output_tokens', 'temperature', 'reasoning_effort', 'supports_images_explicit']) {
      assert.equal(storedCopy[field], sourceBefore[field], `副本持久保留 ${field}`);
    }
    assert.equal(storedCopy.has_api_key, true);
    assert.deepEqual(afterCopy.find(item => item.id === source.id), sourceBefore, '保存副本不改写源卡');
    await noErrors();

    await page.locator(`[data-provider-card="${copied.id}"] .provider-card-name`).click();
    await page.locator('#providerDialog[open]').waitFor();
    assert.equal((await form()).id, copied.id);
    await page.locator('#providerBaseUrl').fill('https://provider-copy-edited.invalid/v1');
    await page.locator('#providerTemperature').fill('0.3');
    await saveDraft();
    const afterEdit = await profiles();
    const edited = afterEdit.find(item => item.id === copied.id);
    assert.equal(edited.base_url, 'https://provider-copy-edited.invalid/v1');
    assert.equal(edited.temperature, 0.3);
    assert.equal(edited.has_api_key, true, '编辑副本时空 Key 按原保存语义保留副本密钥');
    assert.deepEqual(afterEdit.find(item => item.id === source.id), sourceBefore, '编辑副本不影响源配置');
    assert.equal(saves.length, 2);
    assert.equal(saves[1].id, copied.id);
    await noErrors();

    // 源卡的显示密钥响应迟到，不能把旧密钥写入已经打开的复制草稿。
    await page.locator(`[data-provider-card="${source.id}"] .provider-card-name`).click();
    await page.locator('#providerDialog[open]').waitFor();
    let secretArrived, secretRelease;
    const secretStarted = new Promise(resolve => { secretArrived = resolve; });
    secretGate = { arrived: secretArrived, wait: new Promise(resolve => { secretRelease = resolve; }) };
    releaseSecretGate = secretRelease;
    expectedSecretRequests = 1;
    await page.locator('#toggleProviderKey').click();
    await Promise.race([secretStarted, new Promise((_, reject) => setTimeout(() => reject(new Error('源卡显示密钥请求未发出')), 5000))]);
    await closeDraft();
    await copy.click();
    const secretResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/secret'));
    secretRelease();
    releaseSecretGate = null;
    await secretResponse;
    await page.waitForTimeout(100);
    const afterSecret = await form();
    assert.equal(afterSecret.keyEmpty, true, '源卡迟到的密钥响应不污染复制草稿');
    assert.equal(afterSecret.keyType, 'password');
    assert.equal(afterSecret.keyStatus, '未配置');
    assert.equal(afterSecret.id, '');
    assert.equal(secretRequests[0], `/api/providers/${source.id}/secret`);
    await noErrors();

    // 即使手动改回同名且填写相同测试密钥，也必须建立独立供应商，不能被去重当成源卡。
    await page.locator('#providerName').fill(sourceName);
    await page.locator('#providerApiKey').fill(`fixture-source-${width}`);
    const identical = await saveDraft();
    assert.ok(identical.id && identical.id !== source.id && identical.id !== copied.id, '同名同 Key 副本仍有独立 id');
    createdIds.push(identical.id);
    assert.equal(saves.length, 3);
    assert.notEqual(saves[2].id, source.id);
    assert.deepEqual((await profiles()).find(item => item.id === source.id), sourceBefore, '同名同 Key 复制仍不改原供应商');
    await noErrors();

    const local = await seed({
      ...sourceValues, name: `${sourceName}本地`, kind: 'local', request_format: 'llama_cpp',
      base_url: 'http://127.0.0.1:19997/v1', api_key: '', service_manager: 'kvmem',
      service_manager_url: 'http://127.0.0.1:19998',
    });
    await page.reload({ waitUntil: 'networkidle' });
    await openSettings();
    await page.locator('[data-provider-kind="local"]').click();
    assert.equal(await page.locator('#providerCards [data-provider-copy]').count(), await cards.count(), '每张本地供应商卡也有复制按钮');
    await page.locator(`[data-provider-copy="${local.id}"]`).click();
    const localDraft = await form();
    assert.equal(localDraft.id, '');
    assert.equal(localDraft.name, `${sourceName}本地1`);
    assert.equal(localDraft.kind, '1');
    assert.equal(localDraft.format, 'llama_cpp');
    assert.equal(localDraft.baseUrl, 'http://127.0.0.1:19997/v1');
    assert.equal(localDraft.keyEmpty, true);
    assert.equal(await page.locator('#providerKvmemEnabled').isChecked(), true);
    assert.equal(await page.locator('#providerKvmemManagerUrl').inputValue(), 'http://127.0.0.1:19998');
    await closeDraft();
    assert.equal(saves.length, 3, '本地复制取消也不保存');
    await noErrors();
    console.log(`[provider-copy-${width}] 复制预填、空密钥、取消、重名、键盘操作、真实独立保存与编辑全部通过`);
  } catch (error) {
    failures.push(`provider-copy-${width}: ${error.message}`);
    console.log(`[provider-copy-${width}] FAIL ${error.stack || error.message}`);
  } finally {
    if (releaseModelGate) releaseModelGate();
    if (releaseSecretGate) releaseSecretGate();
    await page.close();
    // 即使某个回归意外创建了卡，也只清理本轮唯一名称前缀下的夹具。
    const ownIds = (await profiles()).filter(item => String(item.name || '').startsWith(sourceName)).map(item => item.id);
    for (const id of new Set([...createdIds, ...ownIds].reverse())) {
      await apiJson(`/api/providers/${encodeURIComponent(id)}`, { method: 'DELETE' });
    }
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  for (const width of [1280, 375]) await runProviderCopyViewport(browser, width);
  if (process.env.NAIBA_PROVIDER_COPY_ONLY === '1') {
    await browser.close();
    console.log(`供应商复制专项：${failures.length ? `${failures.length} 项失败` : '全部通过'}`);
    process.exit(failures.length ? 1 : 0);
    return;
  }
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  let fixtureModels = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const url = msg.location()?.url || '';
    let pathname = '';
    try { pathname = new URL(url).pathname; } catch (_) { /* 无 URL 的应用错误照常记录。 */ }
    if (pathname === '/favicon.ico' && msg.text().includes('status of 404 (Not Found)')) return;
    pageErrors.push(`console.error: ${msg.text()}${pathname ? ` @ ${pathname}` : ''}`);
  });
  page.on('response', response => {
    const pathname = new URL(response.url()).pathname;
    if (response.status() < 400 || (response.status() === 404 && pathname === '/favicon.ico')) return;
    pageErrors.push(`HTTP ${response.status()} ${pathname}`);
  });
  page.on('dialog', (dialog) => dialog.accept());
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  // 旧卡片回归也使用本地目录替身；创建/编辑仍请求真实供应商接口，不访问占位上游。
  await page.route('**/api/providers/models', route => {
    const requested = route.request().postDataJSON() || {};
    const models = new Map(fixtureModels.map(model => [model.id, model]));
    if (requested.model) models.set(requested.model, { id: requested.model, name: requested.model });
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ models: [...models.values()] }) });
  });

  let seededIds = [];
  try {
    // ---- 播种：2 个在线 + 1 个本地（前缀可清理）----
    await cleanupSeeded();
    const seeds = [
      { name: `${PREFIX}A`, base_url: 'https://api.example.com/v1', model: 'smoke-model-a', kind: 'online', request_format: 'openai_chat' },
      { name: `${PREFIX}B`, base_url: 'https://api.example.com/v1', model: 'smoke-model-b', kind: 'online', request_format: 'claude' },
      { name: `${PREFIX}本地`, base_url: 'http://127.0.0.1:1234/v1', model: 'smoke-local', kind: 'local', request_format: 'lm_studio' },
    ];
    for (const seed of seeds) {
      const saved = await apiJson('/api/providers', { method: 'POST', body: seed });
      if (saved.id) seededIds.push(saved.id);
    }
    check('播种 3 个供应商成功', seededIds.length === 3, JSON.stringify(seededIds));

    const bootstrap = await apiJson('/api/bootstrap');
    const profiles = bootstrap.model_profiles || [];
    fixtureModels = profiles.filter(provider => provider.model).map(provider => ({
      id: provider.model, name: provider.model,
      context_window: provider.context_window || 32000, max_output_tokens: provider.max_output_tokens || 4096,
    }));
    // 口径：tab 上的卡片数与「该类供应商总数」比较。
    // 只统计播种项等于假设库里本来没有别人，用户一旦存过自定义供应商就必然误报。
    const onlineCount = profiles.filter((p) => (p.kind || 'online') === 'online').length;
    const localCount = profiles.filter((p) => p.kind === 'local').length;
    const kindOf = (id) => profiles.find((p) => p.id === id)?.kind || 'online';
    // 卡片不再展示模型名：弹层核对改用播种数据里的模型名。
    const providerById = new Map((bootstrap.model_profiles || []).map((p) => [p.id, p]));

    // ---- 打开设置 → API 供应商 ----
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(1000);
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="models"]');
    await page.waitForTimeout(400);

    const online = await cardSnapshot(page);
    check('卡片容器存在且渲染供应商卡片 + 添加卡', Boolean(online) && online.total >= 1, JSON.stringify(online));
    check('「添加 API」卡片固定在最后一张', online?.addIsLast === true && online.addText.includes('添加 API'), JSON.stringify(online));
    check('一行最多三张卡片（grid 三列）', online?.columns === 3, String(online?.columns));
    check('在线卡片数量与数据一致',
      online?.providers.length === onlineCount, JSON.stringify({ cards: online?.providers.length, onlineCount }));
    check('每张卡片都有名称/类型标签/右上角 ×',
      online.providers.every((card) => card.name && card.tag && card.hasDelete),
      JSON.stringify(online.providers));
    check('卡片已压成两行（名称 + 脚行），不再展示模型名',
      online.providers.every((card) => card.hasModelText === false),
      JSON.stringify(online.providers.map((card) => card.hasModelText)));
    check('卡片高度按新的 min-height: 88px 收口',
      online.providers.every((card) => card.minHeight === '88px'),
      JSON.stringify(online.providers.map((card) => card.minHeight)));
    check('卡片整张可点（手型光标）',
      online.providers.every((card) => card.cursor === 'pointer'),
      JSON.stringify(online.providers.map((card) => card.cursor)));
    check('默认供应商卡片带「默认」角标',
      online.providers.filter((card) => card.badge === '默认').length === 1,
      JSON.stringify(online.providers.map((card) => card.badge)));

    // ---- 点卡片 → 弹出设置并加载该供应商的预设 ----
    const targetCard = online.providers[0];
    const targetModel = providerById.get(targetCard.id)?.model || '';
    await page.click(`[data-provider-card="${targetCard.id}"] .provider-card-name`);
    await page.waitForTimeout(400);
    let dialog = await dialogSnapshot(page);
    check('点卡片弹出供应商设置弹层', dialog.open === true, JSON.stringify(dialog));
    check('弹层已加载该供应商的预设内容',
      dialog.name === targetCard.name && dialog.model === targetModel && dialog.baseUrl.startsWith('http'),
      JSON.stringify({ dialog, targetCard, targetModel }));
    check('点开即可编辑（字段未禁用）',
      dialog.nameDisabled === false && dialog.formatDisabled === false, JSON.stringify(dialog));
    check('标题为供应商名 + 类型副标题',
      dialog.title === targetCard.name && ['在线 API', '本地 API'].includes(dialog.subtitle),
      JSON.stringify({ title: dialog.title, subtitle: dialog.subtitle }));
    check('旧的「编辑」按钮已移除', dialog.editButtonGone === true, String(dialog.editButtonGone));

    // ---- Esc 关闭并复位 ----
    await page.keyboard.press('Escape');
    await page.waitForTimeout(400);
    dialog = await dialogSnapshot(page);
    check('Esc 关闭弹层', dialog.open === false, JSON.stringify(dialog));
    const afterEsc = await cardSnapshot(page);
    check('关闭后卡片列表仍在（数量不变）', afterEsc?.providers.length === online.providers.length,
      JSON.stringify({ before: online.providers.length, after: afterEsc?.providers.length }));
    const settingsOpen = await page.evaluate(() => Boolean(document.querySelector('#settingsDialog')?.open));
    check('Esc 只关最上层弹层（设置页仍打开）', settingsOpen === true, String(settingsOpen));

    // ---- 添加卡片：新建供应商 ----
    await page.click('[data-provider-add]');
    await page.waitForTimeout(400);
    dialog = await dialogSnapshot(page);
    check('点「添加 API」卡片打开空表单', dialog.open === true && dialog.name === '' && dialog.title.includes('添加'),
      JSON.stringify(dialog));
    await page.fill('#providerName', `${PREFIX}新增`);
    await page.fill('#providerBaseUrl', 'http://127.0.0.1:9999/v1');
    await page.selectOption('#providerModel', '__custom__');
    await page.fill('#providerModelCustom', 'smoke-model-new');
    // 填好上下文/输出上限 → saveProvider 不再去探测不可达的端点（否则要等重试退避）。
    await page.fill('#providerContextWindow', '32000');
    await page.fill('#providerMaxOutputTokens', '4096');
    await page.click('#saveProvider');
    const closed = await waitForClosed(page);
    dialog = await dialogSnapshot(page);
    const afterAdd = await waitForCardCount(page, online.providers.length + 1);
    check('保存后弹层关闭', closed === true && dialog.open === false,
      JSON.stringify({ closed, dialog, error: await page.evaluate(() => document.querySelector('#providerError')?.textContent || '') }));
    check('新供应商卡片出现（数量 +1）',
      afterAdd?.providers.length === online.providers.length + 1,
      JSON.stringify({ before: online.providers.length, after: afterAdd?.providers.length }));
    check('新增卡片排在「添加 API」卡之前', afterAdd?.addIsLast === true, JSON.stringify(afterAdd));

    // ---- × 删除（确认后生效）----
    const created = afterAdd.providers.find((card) => card.name === `${PREFIX}新增`);
    check('新增卡片可按名称定位', Boolean(created), JSON.stringify(afterAdd.providers.map((c) => c.name)));
    await page.click(`[data-provider-delete="${created.id}"]`);
    const afterDelete = await waitForCardCount(page, online.providers.length);
    check('× 删除后卡片消失（数量回落）',
      afterDelete?.providers.length === online.providers.length,
      JSON.stringify({ after: afterDelete?.providers.length, expected: online.providers.length }));
    check('删除后弹层未意外打开', (await dialogSnapshot(page)).open === false, '');

    // ---- 本地 API tab 过滤 ----
    await page.click('[data-provider-kind="local"]');
    await page.waitForTimeout(400);
    const local = await cardSnapshot(page);
    check('切到本地 API 后只显示本地供应商',
      local?.providers.length === localCount && local.providers.every((card) => kindOf(card.id) === 'local'),
      JSON.stringify({ cards: local?.providers.length, localCount, tags: local?.providers.map((c) => c.tag) }));
    check('本地 tab 仍是一行最多三张', local?.columns === 3, String(local?.columns));
    await page.click('[data-provider-kind="online"]');
    await page.waitForTimeout(300);

    // ---- 设置 → 视觉：直接读 API 设置的双下拉，不再有添加/删除 API 按钮 ----
    await page.click('.settings-nav button[data-settings-tab="vision"]');
    await page.waitForTimeout(500);
    const visionUi = await page.evaluate(() => ({
      provider: Boolean(document.querySelector('#visionProvider')),
      model: Boolean(document.querySelector('#visionModel')),
      add: Boolean(document.querySelector('#addVisionProvider')),
      remove: Boolean(document.querySelector('#deleteVisionProvider')),
    }));
    check('视觉页只有供应商+模型双下拉（无添加/删除 API）',
      visionUi.provider && visionUi.model && !visionUi.add && !visionUi.remove,
      JSON.stringify(visionUi));

    // 窄屏列数检查针对供应商卡片网格：先切回「API 供应商」，
    // 否则面板隐藏时 getComputedStyle 拿到未展开的 repeat(...) 字面值，无法判断列数。
    await page.click('.settings-nav button[data-settings-tab="models"]');
    await page.waitForTimeout(400);

    // ---- 窄屏列数降级 ----
    await page.setViewportSize({ width: 900, height: 800 });
    await page.waitForTimeout(300);
    const mid = await cardSnapshot(page);
    check('900px 窗口降级为两列', mid?.columns === 2, String(mid?.columns));
    await page.setViewportSize({ width: 640, height: 800 });
    await page.waitForTimeout(300);
    const narrow = await cardSnapshot(page);
    check('640px 窗口降级为单列', narrow?.columns === 1, String(narrow?.columns));
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.waitForTimeout(200);

    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.stack ? error.stack.split('\n')[0] : error));
  } finally {
    await cleanupSeeded().catch(() => {});
    await browser.close();
  }

  console.log();
  console.log(`API 供应商卡片冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
