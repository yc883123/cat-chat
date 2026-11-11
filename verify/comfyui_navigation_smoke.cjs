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
  let mediaUploads = 0;
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
  await page.route('**/api/file?*', route => route.fulfill({
    status: 200, contentType: 'image/png',
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l1sAAAAASUVORK5CYII=', 'base64'),
  }));
  await page.route('**/api/comfyui/**', route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.includes('/files/')) return route.fulfill({ status: 200, contentType: 'video/mp4', body: '' });
    if (pathname === '/api/comfyui/media') {
      mediaUploads += 1;
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        path: `/managed/uploads/ui-${mediaUploads}.png`, name: `ui-${mediaUploads}.png`, thumb_path: `/managed/uploads/ui-${mediaUploads}_thumb.webp`,
      }) });
    }
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
    await page.locator('#comfyuiRunSection summary[aria-label="更多生成操作"]').click();
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
    // 可见卡片会读取元数据作为封面；未进入预览范围的卡片仍不得发出视频请求。
    // 宽屏可能容得下全部 24 张，按实际滚动区域判断，不能假设所有视口都存在屏外卡片。
    const checkLazyVideos = async () => {
      await page.waitForFunction(() => Array.from(document.querySelectorAll('#comfyuiOutputGrid video')).some(video => video.preload === 'metadata'));
      const videos = await page.locator('#comfyuiOutputGrid video').evaluateAll(nodes => {
        const grid = document.querySelector('#comfyuiOutputGrid').getBoundingClientRect();
        return nodes.map(video => ({
          src: video.src, preload: video.preload,
          farBelow: video.getBoundingClientRect().top > Math.min(grid.bottom, innerHeight) + 240,
        }));
      });
      assert.ok(videos.every(video => ['none', 'metadata'].includes(video.preload)), '视频预览只按需读取元数据');
      const farVideos = videos.filter(video => video.farBelow);
      if (vp.width < 560) assert.ok(farVideos.length > 0, '手机夹具必须包含未滚动到的远处视频');
      assert.ok(farVideos.every(video => video.preload === 'none'), '远离视区的视频不得提前预载');
      const requested = new Set(mediaRequests);
      assert.ok(videos.filter(video => video.preload === 'none').every(video => !requested.has(video.src)), '屏外未预载视频不得发起媒体请求');
    };
    await checkLazyVideos();
    await page.evaluate(() => { window.__comfyuiOldVideos = Array.from(document.querySelectorAll('#comfyuiOutputGrid video')); });
    outputTasks.unshift({
      id: 'cf-video-smoke-new', workflow_name: '新增视频结果', status: 'completed',
      result: { outputs: ['new.mp4'] }, progress: { done: 1, total: 1 },
    });
    await page.waitForFunction(() => document.querySelector('#comfyuiOutputGrid figure')?.dataset.outputKey.startsWith('cf-video-smoke-new'), null, { timeout: 12000 });
    assert.equal(await page.evaluate(() => Array.from(document.querySelectorAll('#comfyuiOutputGrid video')).slice(1).every((video, index) => video === window.__comfyuiOldVideos[index])), true, '插入新结果必须保留全部仍在展示的旧视频 DOM');
    assert.equal(await page.locator('#comfyuiOutputGrid video').count(), 24, '结果仍遵守24个展示上限');
    await checkLazyVideos();
    const lastVideo = page.locator('#comfyuiOutputGrid video').last();
    if (await lastVideo.getAttribute('preload') === 'none') {
      await lastVideo.scrollIntoViewIfNeeded();
      await page.waitForFunction(() => Array.from(document.querySelectorAll('#comfyuiOutputGrid video')).at(-1)?.preload === 'metadata');
    }
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
    // 走真实拖入事件与素材映射控件，校验禁用前口之后的上传和提交都仍属于原节点。
    await page.locator('[data-batch-select="port-node"]').first().selectOption('1|image');
    await page.locator('[data-batch-port-check]').first().check();
    await page.locator('[data-batch-port-check]').nth(1).check();
    const mediaNames = () => page.locator('.cbm-mname').evaluateAll(nodes => nodes.map(node =>
      Array.from(node.childNodes).filter(child => child.nodeType === Node.TEXT_NODE).map(child => child.textContent).join('').trim()));
    const uploadTile = async ordinal => {
      const transfer = await page.evaluateHandle(() => {
        const data = new DataTransfer();
        data.items.add(new File([new Uint8Array([1, 2, 3])], 'fixture.png', { type: 'image/png' }));
        return data;
      });
      const before = mediaUploads;
      await page.locator(`[data-batch-tile][data-port="${ordinal}"]`).dispatchEvent('drop', { dataTransfer: transfer });
      await page.waitForFunction(count => document.querySelectorAll('.cbm-mname').length >= count, ordinal + 1);
      await page.waitForFunction(expected => document.querySelector('#comfyuiBatchBody').innerText.includes(`ui-${expected}.png`), before + 1);
      await transfer.dispose();
    };
    await uploadTile(0);
    await uploadTile(1);
    await page.locator('[data-batch-port-check]').first().uncheck();
    assert.deepEqual(await mediaNames(), ['ui-2.png'], '禁用第一个口只能移除该口文件');
    await uploadTile(0);
    assert.deepEqual(await mediaNames(), ['ui-3.png'], '前口禁用后仍可上传到当前第一个已选口');
    await page.locator('[data-batch-action="port-up"]').nth(1).click();
    assert.equal(await page.locator('[data-batch-select="port-node"]').first().inputValue(), '2|image');
    assert.deepEqual(await mediaNames(), ['ui-3.png'], '排序后文件必须跟随原素材口');
    await page.locator('[data-batch-item="prompt"]').first().fill('用于验证提交与返回轮询的提示词');
    await page.locator('[data-batch-action="submit"]').click();
    await page.locator('.cbm-card .cbm-badge.run').waitFor({ state: 'visible' });
    assert.equal(batchSubmissions.length, 1, '一次点击只提交一个批次');
    assert.equal(batchSubmissions[0].schemes[0].workflow_id, workflow.id);
    assert.equal(batchSubmissions[0].schemes[0].items[0].prompt, '用于验证提交与返回轮询的提示词');
    assert.deepEqual(batchSubmissions[0].schemes[0].items[0].media_inputs, [{ node_id: '2', field_name: 'image', path: '/managed/uploads/ui-3.png', media: 'image' }]);
    await page.evaluate(async () => (await import('/js/22-studio.js')).closeStudioView());
    await page.waitForTimeout(100);
    const closedCount = batchDetailRequests;
    await page.waitForTimeout(2400);
    assert.equal(batchDetailRequests, closedCount, '关闭工作台必须停止批次请求');
    await page.evaluate(async () => (await import('/js/22-studio.js')).openStudioView());
    await page.waitForFunction(() => Boolean(document.querySelector('.cbm-card .cbm-badge.run')));
    await page.waitForTimeout(100);
    assert.ok(batchDetailRequests > closedCount, '重开工作台必须恢复当前批次');
    await page.evaluate(async () => (await import('/js/22-studio.js')).setStudioPage('library'));
    await page.waitForTimeout(100);
    const otherPageCount = batchDetailRequests;
    await page.waitForTimeout(2400);
    assert.equal(batchDetailRequests, otherPageCount, '切换其他工作台页必须停止批次请求');
    await page.evaluate(async () => (await import('/js/22-studio.js')).setStudioPage('comfyui'));
    await page.waitForTimeout(100);
    assert.ok(batchDetailRequests > otherPageCount, '返回造梦机必须恢复批次轮询');
    await checkNoScriptError();
    await paneButton('run').click();
    const beforeReturnRequests = batchDetailRequests;
    batchItemStatus = 'completed';
    await paneButton('batch').click();
    await page.locator('.cbm-card .cbm-badge.done').waitFor({ state: 'visible', timeout: 5000 });
    assert.ok(batchDetailRequests > beforeReturnRequests, '返回运行批次必须立即重新读取服务端状态');
    await checkNoScriptError();
    console.log(`通过 ${vp.width}×${vp.height}：导航、首次进入批量、下游标签、提交及返回轮询、视频按视区懒载及轮询保留DOM`);
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
