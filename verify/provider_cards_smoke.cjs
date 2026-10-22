// API 供应商卡片化冒烟（源码 server，端口 8790）。
// 覆盖：卡片网格一行最多三张 / 末尾固定「添加 API」卡 / 点卡片弹出设置并加载预设 /
//       Esc 与右上角关闭复位 / × 删除走确认 / 添加卡片新建 / 在线-本地 tab 过滤 / 窄屏 2 列与 1 列。
// 前置：源码 server 已在 8790 运行（config.json 已备份，脚本自行播种并在收尾清理）。
// 运行：$env:NODE_PATH="<node_modules 目录>"; node verify\provider_cards_smoke.cjs
const { chromium } = require('playwright');

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

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('dialog', (dialog) => dialog.accept());

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
    check('默认供应商卡片带「当前」角标',
      online.providers.filter((card) => card.badge === '当前').length === 1,
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
