// 造梦机导航与节点说明回归：源码服务 + 离线 API 夹具，覆盖宽桌面、窄桌面和 375px 手机。
// 运行：NAIBA_SMOKE_BASE 指向隔离源码服务；NODE_PATH 指向安装了 Playwright 的目录。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8765';
const workflow = {
  id: 'cw-navigation-smoke', name: '节点映射回归工作流', workflow_fingerprint: 'navigation-smoke-v1',
  node_count: 3, param_count: 3,
  prompt: {
    '1': { class_type: 'LoadImage', inputs: { image: 'None' } },
    '2': { class_type: 'LoadImage', inputs: { image: 'None' } },
    '3': { class_type: 'Textbox', inputs: { text: '示例提示词' } },
  },
  params: [
    { node_id: '1', field_name: 'image', field_type: 'image', control: 'media', media: 'image',
      node_title: '载入图片', label: '1 · 载入图片 · image → ref_images.ref_image_0',
      default: 'None', media_optional: true, connections: [{ target_node_id: '20', target_field: 'ref_images.ref_image_0', output_index: 0 }] },
    { node_id: '2', field_name: 'image', field_type: 'image', control: 'media', media: 'image',
      node_title: '载入图片', label: '2 · 载入图片 · image → ref_images.ref_image_1、mask（节点 21）',
      default: 'None', media_optional: true, connections: [{ target_node_id: '20', target_field: 'ref_images.ref_image_1', output_index: 0 }] },
    { node_id: '3', field_name: 'text', field_type: 'text', control: 'multiline', role: 'positive_prompt',
      node_title: 'Textbox', label: '3 · Textbox · text → prompt', default: '示例提示词' },
  ],
};

async function checkViewport(browser, vp) {
  const page = await browser.newPage({ viewport: vp });
  const errors = [];
  const mediaRequests = [];
  const batchSubmissions = [];
  let batchDetailRequests = 0;
  let batchItemStatus = 'running';
  const runBatch = () => ({
    id: 'cfb-navigation-smoke', title: '浏览器提交回归',
    items: [{ index: 1, name: '回归条目', task_id: 'cf-navigation-item', status: batchItemStatus, done: batchItemStatus === 'completed' ? 1 : 0, total: 1, outputs: [] }],
  });
  const outputTasks = Array.from({ length: 24 }, (_, index) => ({
    id: `cf-video-smoke-${index}`, workflow_name: '视频结果回归', status: 'completed',
    result: { outputs: [`video-${index}.mp4`] }, progress: { done: 1, total: 1 },
  }));
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  page.on('request', request => {
    if (/\/files\/.*\.mp4|\/api\/file\?/.test(request.url())) mediaRequests.push(request.url());
  });
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  page.on('response', response => {
    if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) errors.push(`HTTP ${response.status()} ${response.url()}`);
  });
  const checkNoScriptError = async () => {
    assert.deepEqual(errors, [], '页面不得产生脚本或接口错误');
    assert.equal(await page.locator('body').evaluate(el => /JS ERROR|PROMISE REJECT/.test(el.innerText)), false);
  };
  await page.route('**/api/providers/models', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ models: [] }),
  }));
  await page.route('**/api/comfyui/**', route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.includes('/files/')) return route.fulfill({ status: 200, contentType: 'video/mp4', body: '' });
    if (pathname === '/api/comfyui/batches' && route.request().method() === 'POST') {
      batchSubmissions.push(route.request().postDataJSON());
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ batch: runBatch() }) });
    }
    if (pathname === '/api/comfyui/batches/cfb-navigation-smoke') {
      batchDetailRequests += 1;
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ batch: runBatch() }) });
    }
    const responses = {
      '/api/comfyui/health': { ok: true, url: 'http://127.0.0.1:8188', devices: [] },
      '/api/comfyui/models': { models: [], loras: [] },
      '/api/comfyui/workflows': { workflows: [workflow] },
      [`/api/comfyui/workflows/${workflow.id}`]: { workflow },
      '/api/comfyui/tasks': { tasks: outputTasks },
      '/api/comfyui/batches': { batches: [] },
    };
    assert.ok(Object.hasOwn(responses, pathname), `未预期的 ComfyUI 请求：${pathname}`);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(responses[pathname]) });
  });
  try {
    await page.goto(`${BASE}/`, { waitUntil: 'networkidle' });
    assert.equal(await page.title(), 'Cat Chat');
    await page.evaluate(async () => {
      const studio = await import('/js/22-studio.js');
      studio.openStudioView();
      studio.setStudioPage('comfyui');
    });
    const paneButton = pane => page.locator(`#comfyuiPanes [data-comfyui-pane="${pane}"]`);
    await page.locator('#comfyuiWorkflowList .comfyui-workflow-title').waitFor({ state: 'attached' });
    assert.equal(await page.locator('#comfyuiPanes').isVisible(), true, '所有视口均需显示导航');
    assert.deepEqual(await page.locator('#comfyuiPanes button').allTextContents(), ['工作流', '单次生成', '批量生成', '结果']);
    await checkNoScriptError();

    await paneButton('workflows').click();
    await page.locator('#comfyuiWorkflowList .comfyui-workflow-title').click();
    await page.locator('#comfyuiParamForm [data-comfyui-slot="3|text"]').waitFor({ state: 'visible' });
    assert.equal(await page.locator('[data-comfyui-media-tile="1|image"]').getAttribute('title'), `${workflow.params[0].label}\n未选即跳过`);
    await checkNoScriptError();

    // 首次直接点击快捷入口，不能只有切档、没有初始化填单。
    await page.locator('#comfyuiOpenBatch').click();
    await page.locator('#comfyuiBatchBody [data-batch-select="workflow"]').waitFor({ state: 'visible' });
    assert.equal(await paneButton('batch').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.locator('#comfyuiColumns').isVisible(), false);
    await checkNoScriptError();
    await page.locator('#comfyuiBatchBody [data-batch-select="workflow"]').selectOption(workflow.id);
    const nodeSelect = page.locator('[data-batch-select="port-node"]').first();
    await nodeSelect.waitFor({ state: 'visible', timeout: 5000 });
    assert.deepEqual(await nodeSelect.locator('option').allTextContents(), workflow.params.slice(0, 2).map(slot => slot.label));
    await nodeSelect.selectOption('2|image');
    assert.equal(await page.locator('[data-batch-select="port-node"]').first().inputValue(), '2|image');
    await checkNoScriptError();

    await paneButton('tasks').click();
    assert.equal(await page.locator('.comfyui-results-col').isVisible(), true);
    assert.equal(await page.locator('#comfyuiBatch').isVisible(), false);
    assert.equal(await page.locator('#comfyuiOutputGrid video').count(), 24);
    assert.equal(await page.locator('#comfyuiOutputGrid video').evaluateAll(nodes => nodes.every(node => node.preload === 'none')), true);
    await page.evaluate(() => { window.__comfyuiOldVideo = document.querySelector('#comfyuiOutputGrid video'); });
    outputTasks.unshift({
      id: 'cf-video-smoke-new', workflow_name: '新增视频结果', status: 'completed',
      result: { outputs: ['new.mp4'] }, progress: { done: 1, total: 1 },
    });
    await page.waitForFunction(() => document.querySelector('#comfyuiOutputGrid figure')?.dataset.outputKey.startsWith('cf-video-smoke-new'), null, { timeout: 12000 });
    assert.equal(await page.evaluate(() => document.querySelectorAll('#comfyuiOutputGrid video')[1] === window.__comfyuiOldVideo), true, '插入新结果必须保留旧视频 DOM');
    assert.equal(await page.locator('#comfyuiOutputGrid video').count(), 24, '结果仍遵守24个展示上限');
    assert.deepEqual(mediaRequests, [], '未点击播放时不得自动请求24段视频');
    await checkNoScriptError();
    await paneButton('run').click();
    assert.equal(await page.locator('.comfyui-run-col').isVisible(), true);
    await paneButton('batch').click();
    assert.equal(await page.locator('[data-batch-select="port-node"]').first().inputValue(), '2|image', '往返页签保留素材口映射');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, '手机导航不能横向溢出');
    if (vp.width < 560) {
      assert.equal(await page.locator('.cbm-media-slot').evaluateAll(nodes => nodes.every(node => node.getBoundingClientRect().height < 100)), true, '手机素材口说明不能挤成逐字竖排');
    }
    await checkNoScriptError();
    await page.screenshot({ path: path.join(__dirname, `comfyui_navigation_${vp.width}.png`) });
    await page.locator('[data-batch-item="prompt"]').first().fill('用于验证提交与返回轮询的提示词');
    await page.locator('[data-batch-action="submit"]').click();
    await page.locator('.cbm-card .cbm-badge.run').waitFor({ state: 'visible' });
    assert.equal(batchSubmissions.length, 1, '一次点击只提交一个批次');
    assert.equal(batchSubmissions[0].schemes[0].workflow_id, workflow.id);
    assert.equal(batchSubmissions[0].schemes[0].items[0].prompt, '用于验证提交与返回轮询的提示词');
    await checkNoScriptError();
    await paneButton('run').click();
    const beforeReturnRequests = batchDetailRequests;
    batchItemStatus = 'completed';
    await paneButton('batch').click();
    await page.locator('.cbm-card .cbm-badge.done').waitFor({ state: 'visible', timeout: 5000 });
    assert.ok(batchDetailRequests > beforeReturnRequests, '返回运行批次必须立即重新读取服务端状态');
    await checkNoScriptError();
    console.log(`通过 ${vp.width}×${vp.height}：导航、首次进入批量、下游标签、提交及返回轮询、24段视频零预载及轮询保留DOM`);
  } catch (error) {
    console.error('批量区状态：', await page.locator('#comfyuiBatchBody').innerText(), errors);
    await page.screenshot({ path: path.join(__dirname, `comfyui_navigation_failed_${vp.width}.png`) });
    throw error;
  } finally {
    await page.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const vp of [{ width: 1680, height: 1000 }, { width: 1280, height: 800 }, { width: 375, height: 812 }]) await checkViewport(browser, vp);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
