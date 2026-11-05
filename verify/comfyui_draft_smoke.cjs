// 隔离源码服务 + 离线接口夹具，覆盖批量草稿的真实输入、刷新、恢复和存储失败。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8765';
const DRAFT_KEY = 'cfBatchDraft:v1';
const image = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l1sAAAAASUVORK5CYII=', 'base64');
const workflow = {
  id: 'cw-draft-smoke', name: '草稿恢复回归工作流', workflow_fingerprint: 'draft-smoke-v1',
  node_count: 2, param_count: 2,
  prompt: {
    '1': { class_type: 'LoadImage', inputs: { image: 'None' } },
    '2': { class_type: 'Textbox', inputs: { text: '默认提示词' } },
  },
  params: [
    { node_id: '1', field_name: 'image', field_type: 'image', control: 'media', media: 'image',
      node_title: '载入图片', label: '1 · 载入图片 · image → reference', default: 'None', media_optional: false },
    { node_id: '2', field_name: 'text', field_type: 'text', control: 'multiline', role: 'positive_prompt',
      node_title: 'Textbox', label: '2 · Textbox · text → prompt', default: '默认提示词' },
  ],
};

async function checkViewport(browser, viewport) {
  workflow.workflow_fingerprint = 'draft-smoke-v1';
  const page = await browser.newPage({ viewport });
  const errors = [];
  let mediaChecks = 0;
  let holdDetail = false;
  let releaseDetail = null;
  let heldDetail = false;
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/api/bootstrap', async route => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.data_location = { ...payload.data_location, data_dir: '/managed' };
    return json(route, payload);
  });
  await page.route('**/api/providers/models', route => json(route, { models: [] }));
  await page.route('**/api/file?*', route => route.fulfill({ status: 200, contentType: 'image/png', body: image }));
  await page.route('**/api/uploads/check', route => {
    mediaChecks += 1;
    return json(route, { missing: [] });
  });
  await page.route('**/api/comfyui/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === `/api/comfyui/workflows/${workflow.id}`) {
      if (holdDetail) {
        heldDetail = true;
        await new Promise(resolve => { releaseDetail = resolve; });
      }
      return json(route, { workflow });
    }
    if (url.pathname === '/api/comfyui/media') return json(route, {
      path: '/managed/uploads/draft-fixture.png', name: 'draft-fixture.png', thumb_path: '/managed/uploads/draft-fixture-thumb.webp',
    });
    const responses = {
      '/api/comfyui/health': { ok: true, url: 'http://127.0.0.1:8188', devices: [] },
      '/api/comfyui/models': { models: [], loras: [] },
      '/api/comfyui/workflows': { workflows: [workflow] },
      '/api/comfyui/tasks': { tasks: [] },
      '/api/comfyui/batches': { batches: [] },
    };
    assert.ok(Object.hasOwn(responses, url.pathname), `未预期的请求：${url.pathname}`);
    return json(route, responses[url.pathname]);
  });
  const item = field => page.locator(`[data-batch-item="${field}"]`).first();
  const openDraft = async () => {
    await page.evaluate(async () => {
      const studio = await import('/js/22-studio.js');
      studio.openStudioView();
      studio.setStudioPage('comfyui');
    });
    await page.locator('#comfyuiPanes [data-comfyui-pane="batch"]').click();
    await page.locator('[data-batch-select="workflow"]').first().waitFor({ state: 'visible' });
  };
  const saved = async () => {
    await page.waitForFunction(key => {
      const raw = localStorage.getItem(key);
      return raw && raw.includes('人工剧本') && raw.includes('恢复提示词');
    }, DRAFT_KEY);
  };
  const noErrors = async () => {
    assert.deepEqual(errors, [], '草稿操作不得产生脚本错误');
    assert.equal(await page.locator('body').evaluate(el => /JS ERROR|PROMISE REJECT/.test(el.innerText)), false);
  };
  page.on('dialog', dialog => dialog.accept());
  try {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await openDraft();
    await page.locator('[data-batch-select="workflow"]').first().selectOption(workflow.id);
    await item('script').waitFor({ state: 'visible' });
    await page.locator('[data-batch-field="title"]').fill('自动恢复验收批次');
    await item('script').fill('人工剧本：少女走进月光下的花园。');
    await item('prompt').fill('恢复提示词：月光花园，保持首帧构图。');
    const transfer = await page.evaluateHandle(() => {
      const data = new DataTransfer();
      data.items.add(new File([new Uint8Array([1, 2, 3])], 'draft-fixture.png', { type: 'image/png' }));
      return data;
    });
    await page.locator('[data-batch-tile]').first().dispatchEvent('drop', { dataTransfer: transfer });
    await transfer.dispose();
    await page.locator('.cbm-mname').first().waitFor({ state: 'visible' });
    await saved();
    await page.reload({ waitUntil: 'networkidle' });
    await openDraft();
    await page.waitForFunction(() => document.querySelector('[data-batch-item="script"]')?.value.includes('人工剧本'));
    assert.equal(await page.locator('[data-batch-field="title"]').inputValue(), '自动恢复验收批次');
    assert.equal(await item('prompt').inputValue(), '恢复提示词：月光花园，保持首帧构图。');
    await page.locator('.cbm-mname').first().waitFor({ state: 'visible' });
    assert.ok(mediaChecks > 0, '恢复已上传素材时必须向服务端复核路径');
    await noErrors();

    // 不等防抖时间直接刷新，页面退出时仍应同步保存最后一次人工输入。
    await item('script').fill('人工剧本：刷新前最后一次输入。');
    await page.reload({ waitUntil: 'networkidle' });
    await openDraft();
    await page.waitForFunction(() => document.querySelector('[data-batch-item="script"]')?.value === '人工剧本：刷新前最后一次输入。');

    // 工作流已被外部改过时保留文字，旧素材与参数不能未经核对继续提交。
    workflow.workflow_fingerprint = 'draft-smoke-v2';
    await page.reload({ waitUntil: 'networkidle' });
    await openDraft();
    await page.locator('[data-batch-action="confirm-restored-workflow"]').waitFor({ state: 'visible' });
    assert.equal(await item('script').inputValue(), '人工剧本：刷新前最后一次输入。');
    assert.equal(await page.locator('.cbm-mname').count(), 0, '工作流变化后旧素材不能误连到新口');
    await page.locator('[data-batch-action="confirm-restored-workflow"]').click();

    // 配额不足必须给出反馈，并保留当前可编辑内容。
    await page.evaluate(key => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function (name, value) {
        if (name === key) throw new DOMException('模拟本机空间不足', 'QuotaExceededError');
        return original.call(this, name, value);
      };
      window.__restoreDraftStorage = () => { Storage.prototype.setItem = original; };
    }, DRAFT_KEY);
    await item('prompt').fill('本机空间不足时仍保留人工编辑');
    await page.locator('[data-batch-draft-status]').filter({ hasText: /未保存|保存失败/ }).waitFor({ state: 'visible' });
    assert.equal(await item('prompt').inputValue(), '本机空间不足时仍保留人工编辑');
    await page.evaluate(() => window.__restoreDraftStorage());
    await noErrors();

    // 恢复请求迟到时，不能复活已经清空的方案和草稿。
    holdDetail = true;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await openDraft();
    await page.waitForFunction(() => Boolean(document.querySelector('[data-batch-action="clear-draft"]')));
    const deadline = Date.now() + 5000;
    while (!heldDetail && Date.now() < deadline) await page.waitForTimeout(50);
    assert.equal(heldDetail, true, '必须实际挂起恢复工作流详情请求');
    await page.locator('[data-batch-action="clear-draft"]').click();
    holdDetail = false;
    releaseDetail();
    await page.waitForTimeout(700);
    assert.equal(await page.locator('[data-batch-select="workflow"]').first().inputValue(), '', '迟到回包不能恢复清空的工作流');
    await page.reload({ waitUntil: 'networkidle' });
    await openDraft();
    assert.equal(await page.locator('[data-batch-select="workflow"]').first().inputValue(), '', '清空后刷新不能复活旧草稿');
    assert.equal(await page.locator('[data-batch-field="title"]').inputValue(), '');

    // 损坏的本机草稿不能阻止打开批量页面。
    await page.evaluate(key => localStorage.setItem(key, '{损坏的JSON'), DRAFT_KEY);
    await page.reload({ waitUntil: 'networkidle' });
    await openDraft();
    await page.locator('[data-batch-action="add-scheme"]').waitFor({ state: 'visible' });
    await noErrors();
    await page.screenshot({ path: path.join(__dirname, `comfyui_draft_${viewport.width}.png`) });
    console.log(`通过 ${viewport.width}×${viewport.height}：草稿自动保存、退出刷新、素材复核、空间不足提示、清空竞态和坏数据恢复`);
  } catch (error) {
    if (releaseDetail) releaseDetail();
    await page.screenshot({ path: path.join(__dirname, `comfyui_draft_failed_${viewport.width}.png`) });
    throw error;
  } finally {
    await page.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const viewport of [{ width: 1280, height: 800 }, { width: 375, height: 812 }]) await checkViewport(browser, viewport);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
