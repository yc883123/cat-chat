// 提词库反推与批量 H3 转换：真实页面、离线模型/上传夹具；桌面及 375px。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8765';
const PIXEL = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64');
const workflow = {
  id: 'cw-convert-feedback', name: 'H3 转换验收', workflow_fingerprint: 'h3-convert-feedback',
  prompt: { '1': { inputs: { image: 'None' } }, '2': { inputs: { text: '默认提示词' } }, '3': { inputs: { seconds: 8 } } },
  params: [
    { node_id: '1', field_name: 'image', field_type: 'image', media: 'image', control: 'media', media_optional: false, default: 'None', label: '1 · 首帧 → H3.image' },
    { node_id: '2', field_name: 'text', field_type: 'text', control: 'multiline', role: 'positive_prompt', label: '2 · 提示词', default: '默认提示词' },
    { node_id: '3', field_name: 'seconds', field_type: 'number', label: '3 · duration 时长', default: 8 },
  ],
};

async function runViewport(browser, viewport) {
  const page = await browser.newPage({ viewport });
  const errors = [];
  const generated = [];
  const tasks = new Map();
  let uploadCount = 0;
  let visionDelay = 0;
  const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  const check = async () => {
    assert.deepEqual(errors, [], '无脚本与接口错误');
    assert.equal(await page.locator('body').evaluate(el => /JS ERROR|PROMISE REJECT/.test(el.innerText)), false);
  };
  const upload = () => ({ path: `uploads/feedback-${++uploadCount}.png`, name: `图${uploadCount}.png`, thumb_path: '' });
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text()); });
  page.on('response', response => { if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) errors.push(`${response.status()} ${response.url()}`); });
  await page.addInitScript(() => { localStorage.clear(); localStorage.setItem('naibaOnboardingDismissed', '1'); });
  await page.route('**/api/providers/models', route => json(route, { models: [{ id: 'probe-default', name: '本地视觉替身' }] }));
  await page.route('**/api/settings', route => route.request().method() === 'POST'
    ? json(route, { settings: route.request().postDataJSON() }) : route.continue());
  await page.route('**/api/upload*', route => json(route, upload()));
  await page.route('**/api/file*', route => route.fulfill({ status: 200, contentType: 'image/png', body: PIXEL }));
  await page.route('**/api/studio/**', async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.includes('/catalog')) return route.continue();
    if (pathname === '/api/studio/custom') return json(route, { items: [] });
    if (pathname === '/api/studio/generate') {
      const body = route.request().postDataJSON();
      generated.push(body);
      const task = { id: `st-feedback-${generated.length}`, status: 'done', input: body, title: '转换结果',
        result: body.item_id === 'h3-014-h3-ref2va'
          ? 'subject_definitions: reference\nsummary: reference generation\nretention_analysis: fully_preserved\ndetailed_description: [Shot 1] A cat sits.\noverall_soundscape: Wind.\nnon_diegetic_music: N/A'
          : 'integrated_multimodal_description: [Shot 1] A cat sits.\noverall_soundscape: Wind.\nnon_diegetic_music: N/A',
        usage: {}, created_at: Date.now(), updated_at: Date.now(), result_partial: '', reasoning: [], performance: {} };
      tasks.set(task.id, task);
      return json(route, { task });
    }
    if (pathname === '/api/studio/vision') {
      const body = route.request().postDataJSON();
      if (visionDelay) await new Promise(resolve => setTimeout(resolve, visionDelay));
      return json(route, { analyzed: body.images.length, failed: 0, fallback_used: 0,
        text: '【图1】\n一只橘猫坐在窗边', images: [{ index: 1, text: '一只橘猫坐在窗边', error: '', backend: 'selected' }] });
    }
    if (pathname === '/api/studio/tasks') return json(route, { tasks: [] });
    const task = tasks.get(pathname.split('/')[4]);
    if (pathname.endsWith('/events')) return json(route, { events: [], status: 'done', last_seq: 0 });
    return json(route, { task });
  });
  await page.route('**/api/comfyui/**', route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/api/comfyui/media') return json(route, upload());
    return json(route, {
      '/api/comfyui/health': { ok: true, url: 'http://127.0.0.1:8188' },
      '/api/comfyui/models': { models: [], loras: [] },
      '/api/comfyui/workflows': { workflows: [workflow] },
      [`/api/comfyui/workflows/${workflow.id}`]: { workflow },
      '/api/comfyui/tasks': { tasks: [] }, '/api/comfyui/batches': { batches: [] },
    }[pathname] || {});
  });
  const pane = async name => {
    if (viewport.width <= 760) await page.locator(`#studioPanes [data-studio-pane="${name}"]`).click();
  };
  try {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await page.evaluate(async () => {
      const studio = await import('/js/22-studio.js'); studio.openStudioView(); studio.setStudioPage('library');
    });
    await pane('list');
    await page.locator('#studioCats [data-studio-module="vi"]').click();
    await page.locator('#studioList [data-studio-pick="vi-004-cmd-reverse"]').click();
    await check();
    assert.match(await page.locator('#studioBodyPreview').textContent(), /实际收到的参考图片/);
    await page.locator('#studioImagePick').click();
    await page.locator('#studioImageFileInput').setInputFiles({ name: 'cat.png', mimeType: 'image/png', buffer: PIXEL });
    await page.locator('#studioImageConfirm').click();
    await page.locator('#studioProviderSelect').selectOption('online:probe-te');
    await page.locator('#studioModelSelect').selectOption('probe-default');
    await page.locator('#studioUserRequest').fill('');
    await page.locator('#studioGenerate').click();
    await page.waitForFunction(() => document.querySelector('#studioResultStream')?.textContent.includes('integrated_multimodal_description'));
    assert.equal(generated[0].item_id, 'vi-004-cmd-reverse');
    assert.equal(generated[0].user_request, '');
    assert.equal(generated[0].images.length, 1);
    await pane('form');
    await check();
    await page.locator('#studioVisionAnalyze').click();
    await page.waitForFunction(() => document.querySelector('#studioImageStatus').textContent.includes('已识别 1/1 张'));
    assert.match(await page.locator('#studioUserRequest').inputValue(), /一只橘猫/);
    assert.match(await page.locator('#studioImageStatus').textContent(), /当前模型/);
    await check();
    const inputBefore = await page.locator('#studioUserRequest').inputValue();
    visionDelay = 500;
    await page.locator('#studioVisionAnalyze').click();
    await page.locator('#studioImagePreview .studio-image-remove').click();
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('参考图片已更换'));
    assert.equal(await page.locator('#studioUserRequest').inputValue(), inputBefore);
    assert.equal(await page.locator('#studioImageStatus').textContent(), '');
    await check();

    await page.evaluate(async () => { (await import('/js/22-studio.js')).setStudioPage('comfyui'); });
    await page.locator('#comfyuiPanes [data-comfyui-pane="batch"]').click();
    await page.locator('[data-batch-select="workflow"]').selectOption(workflow.id);
    await page.locator('[data-batch-item="script"]').fill('橘猫转头望向窗外，首帧按图1');
    await page.locator('[data-batch-field="convertTemplate"]').waitFor({ state: 'visible' });
    await page.locator('[data-batch-field="convertProvider"]').selectOption('online:probe-te');
    await page.locator('[data-batch-field="convertModel"]').selectOption('probe-default');
    const chooserPromise = page.waitForEvent('filechooser');
    await page.locator('[data-batch-tile]').first().click();
    await (await chooserPromise).setFiles({ name: 'batch-cat.png', mimeType: 'image/png', buffer: PIXEL });
    await page.locator('[data-batch-tile] img').waitFor({ state: 'visible' });
    assert.equal(await page.locator('[data-batch-field="convertModel"]').inputValue(), 'probe-default', '上传重绘保留选中模型');
    await check();
    await page.locator('[data-batch-action="convert-item"]').first().click();
    await page.waitForFunction(() => document.querySelector('[data-batch-item="prompt"]')?.value.includes('integrated_multimodal_description'));
    const base = generated.at(-1);
    assert.equal(base.item_id, 'h3-007-h3-i2va');
    assert.equal(base.images.length, 1);
    assert.match(base.images[0].path, /feedback-\d+.png/);
    assert.match(base.user_request, /图1：节点 1，字段 image，标签 1 · 首帧/);
    assert.match(base.user_request, /目标时长：8 秒/);
    await check();
    await page.locator('[data-batch-item="prompt"]').fill('');
    await page.locator('[data-batch-field="convertTemplate"]').selectOption('builtin:h3-ref2va');
    await page.locator('[data-batch-action="convert-item"]').first().click();
    await page.waitForFunction(() => document.querySelector('[data-batch-item="prompt"]')?.value.includes('subject_definitions'));
    assert.equal(generated.at(-1).item_id, 'h3-014-h3-ref2va');
    assert.equal(generated.at(-1).images.length, 1);
    assert.equal(await page.locator('[data-batch-field="convertTemplate"]').inputValue(), 'builtin:h3-ref2va', '转换重绘保留参考模板');
    await check();
    await page.screenshot({ path: path.join(__dirname, `studio_prompt_feedback_${viewport.width}.png`) });
    console.log(`通过 ${viewport.width}px：图片模板无文字生成、识别状态、迟到图片隔离、批量真实图片与base/ref模板选择`);
  } catch (error) {
    await page.screenshot({ path: path.join(__dirname, `studio_prompt_feedback_failed_${viewport.width}.png`) });
    throw error;
  } finally { await page.close(); }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try { for (const viewport of [{ width: 1280, height: 900 }, { width: 375, height: 812 }]) await runViewport(browser, viewport); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
