// 隔离源码服务 + 离线接口夹具：批量灯箱、多选删除和 Skill 载入保护。
// NAIBA_SMOKE_BASE 指向隔离服务；NAIBA_SMOKE_VIDEO_FILE 可提供短视频用于播放器切换验证。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8765';
const OUTPUT = path.resolve(process.env.NAIBA_SMOKE_OUTPUT || __dirname);
fs.mkdirSync(OUTPUT, { recursive: true });
const video = process.env.NAIBA_SMOKE_VIDEO_FILE ? fs.readFileSync(process.env.NAIBA_SMOKE_VIDEO_FILE) : null;
const image = '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800"><rect width="1280" height="800" fill="#7094bd"/><circle cx="640" cy="400" r="180" fill="#efdad0"/></svg>';

async function runViewport(browser, viewport) {
  const page = await browser.newPage({ viewport, hasTouch: viewport.width < 500 });
  const errors = [], deleteRequests = [], confirmations = [];
  let mode = 'image', batchExists = true, failOne = false, retryMode = false;
  let holdNextBatch = false, releaseBatch = null;
  let delayedSkill = null, skillRequested = false, longSkill = false;
  const deleted = new Set();
  const names = () => [1, 2, 3].map(index => `${mode}-${index}.${mode === 'video' ? 'mp4' : 'png'}`);
  const batch = () => ({
    id: 'ui-preview-batch', title: '预览与删除回归批次', item_count: 3, done_count: 3 - deleted.size,
    created_at: '2026-11-01T00:00:00Z',
    items: names().map((name, index) => ({ index: index + 1, name: `条目 ${index + 1}`,
      task_id: `ui-task-${index + 1}`, status: deleted.has(`ui-task-${index + 1}`) ? 'missing'
        : retryMode && index === 0 ? 'failed' : 'completed',
      done: 1, total: 1, outputs: deleted.has(`ui-task-${index + 1}`) ? [] : [name] })),
  });
  const json = (route, data) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  page.on('dialog', async dialog => { confirmations.push(dialog.message()); await dialog.accept(); });
  await page.addInitScript(() => { localStorage.clear(); localStorage.setItem('naibaOnboardingDismissed', '1'); });
  await page.route('**/api/providers/models', route => json(route, { models: [] }));
  await page.route('**/api/skills/scan', route => json(route, { skills: [{ id: 'ui-skill', name: '回归用 Skill', char_count: 30 }] }));
  await page.route('**/api/skills/content', async route => {
    skillRequested = true;
    if (!longSkill) await new Promise(resolve => { delayedSkill = resolve; });
    return json(route, { id: 'ui-skill', name: '回归用 Skill', content: longSkill ? '长'.repeat(12001) : '旧 Skill 返回的内容', body: '正文' });
  });
  await page.route('**/api/file?**', route => route.fulfill({ status: 200, contentType: 'image/svg+xml', body: image }));
  await page.route('**/api/comfyui/**', route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.includes('/files/')) return route.fulfill({
      status: 200, contentType: url.pathname.endsWith('.mp4') ? 'video/mp4' : 'image/svg+xml',
      body: url.pathname.endsWith('.mp4') ? (video || Buffer.alloc(0)) : image,
    });
    if (url.pathname === '/api/comfyui/tasks' && request.method() === 'DELETE') {
      const body = request.postDataJSON();
      deleteRequests.push(body);
      const failed = [];
      let count = 0;
      for (const id of body.task_ids) {
        if (failOne && id === 'ui-task-2') { failed.push({ id, reason: '测试夹具：远端停止尚未确认' }); continue; }
        deleted.add(id); count += 1;
      }
      return json(route, { deleted: count, failed, skipped: [] });
    }
    if (url.pathname === '/api/comfyui/batches/ui-preview-batch' && request.method() === 'DELETE') {
      batchExists = false;
      return json(route, { ok: true, deleted: 3, failed: [], skipped: [] });
    }
    if (url.pathname === '/api/comfyui/tasks/ui-task-1/retry') {
      holdNextBatch = true;
      return json(route, { task: { id: 'ui-task-1', status: 'queued' } });
    }
    if (url.pathname === '/api/comfyui/batches/ui-preview-batch' && holdNextBatch) {
      holdNextBatch = false;
      const pending = batch();
      return new Promise(resolve => { releaseBatch = () => resolve(json(route, { batch: pending })); });
    }
    const taskMatch = url.pathname.match(/^\/api\/comfyui\/tasks\/(ui-task-[123])$/);
    if (taskMatch) {
      const index = Number(taskMatch[1].slice(-1)) - 1, name = names()[index];
      return json(route, { task: { id: taskMatch[1], status: 'completed',
        output_files: deleted.has(taskMatch[1]) ? [] : [{ name, path: `/managed/generated/${name}` }] } });
    }
    const responses = {
      '/api/comfyui/health': { ok: true, url: 'http://127.0.0.1:8188', devices: [] },
      '/api/comfyui/models': { models: [], loras: [] },
      '/api/comfyui/workflows': { workflows: [] },
      '/api/comfyui/tasks': { tasks: [] },
      '/api/comfyui/batches': { batches: batchExists ? [batch()] : [] },
      '/api/comfyui/batches/ui-preview-batch': { batch: batch() },
    };
    assert.ok(Object.hasOwn(responses, url.pathname), `未预期的接口：${url.pathname}`);
    return json(route, responses[url.pathname]);
  });

  const openBatch = async () => {
    await page.evaluate(async () => {
      const studio = await import('/js/22-studio.js');
      studio.openStudioView(); studio.setStudioPage('comfyui');
    });
    await page.locator('#comfyuiPanes [data-comfyui-pane="tasks"]').click();
    await page.locator('[data-comfyui-results-seg="batch"]').click();
    await page.locator('[data-batch-open="ui-preview-batch"]').click();
    await page.waitForFunction(() => document.querySelectorAll('[data-batch-task-id]').length === 3);
  };
  const verifyErrors = async () => {
    assert.deepEqual(errors, [], '交互不得触发脚本错误');
    assert.equal(await page.locator('body').evaluate(node => /JS ERROR|PROMISE REJECT/.test(node.innerText)), false);
  };
  try {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await openBatch();
    await page.locator('[data-batch-action="toggle-item-outputs"]').first().click();
    await page.locator('[data-batch-expand-for="ui-task-1"] img[data-large-url]').waitFor({ state: 'visible' });
    // 手机无需先滚到屏外的卡片，当前批次已知的图片就应加入切换列表。
    const firstImage = page.locator('.cbm-card-preview img[data-large-url]').first();
    await firstImage.waitFor({ state: 'visible' });
    await firstImage.click();
    await page.locator('#imageLightbox').waitFor({ state: 'visible' });
    assert.equal((await page.locator('#imageLightboxCounter').innerText()).trim(), '1 / 3',
      JSON.stringify(await firstImage.evaluate(async element => (await import('/js/03-media.js')).collectMediaPreviewItems(element))));
    await page.locator('#imageLightboxNext').click();
    assert.equal((await page.locator('#imageLightboxCounter').innerText()).trim(), '2 / 3');
    await page.keyboard.press('ArrowRight');
    assert.equal((await page.locator('#imageLightboxCounter').innerText()).trim(), '3 / 3');
    await page.keyboard.press('ArrowLeft');
    if (viewport.width < 500) {
      await page.locator('#imageLightbox').evaluate(box => {
        const touch = x => new Touch({ identifier: 1, target: box, clientX: x, clientY: 300 });
        box.dispatchEvent(new TouchEvent('touchstart', { bubbles: true, cancelable: true, touches: [touch(300)] }));
        box.dispatchEvent(new TouchEvent('touchmove', { bubbles: true, cancelable: true, touches: [touch(100)] }));
        box.dispatchEvent(new TouchEvent('touchend', { bubbles: true, cancelable: true, touches: [], changedTouches: [touch(100)] }));
      });
      assert.equal((await page.locator('#imageLightboxCounter').innerText()).trim(), '3 / 3', '手机单指水平滑动切到下一张');
    }
    // 模拟桌面桥，验证翻页后的下载仍传入对应文件的本地路径。
    await page.evaluate(() => {
      window.__originalPywebview = window.pywebview;
      window.__nativeSaves = [];
      window.pywebview = { api: { naibaSaveFileAs: (file, name) => {
        window.__nativeSaves.push({ file, name }); return { ok: true };
      } } };
    });
    await page.keyboard.press('Escape');
    await firstImage.click();
    await page.locator('#imageLightboxDownload').click();
    await page.waitForFunction(() => window.__nativeSaves.length === 1);
    await page.locator('#imageLightboxNext').click();
    await page.locator('#imageLightboxDownload').click();
    await page.waitForFunction(() => window.__nativeSaves.length === 2);
    await page.locator('#imageLightboxNext').click();
    await page.locator('#imageLightboxDownload').click();
    await page.waitForFunction(() => window.__nativeSaves.length === 3);
    assert.deepEqual(await page.evaluate(() => window.__nativeSaves.map(row => row.file)),
      [1, 2, 3].map(index => `/managed/generated/image-${index}.png`));
    await page.evaluate(() => { window.pywebview = window.__originalPywebview; });
    await page.keyboard.press('Escape');
    await page.locator('#imageLightbox').waitFor({ state: 'hidden' });

    const deleteButton = page.locator('[data-batch-action="delete-item"]').first();
    await deleteButton.waitFor({ state: 'visible' });
    assert.match(await deleteButton.innerText(), /删除产物/);
    const target = await deleteButton.boundingBox();
    assert.ok(target.width >= 44 && target.height >= 44, '卡片删除按钮必须有足够点击面积');
    await page.locator('[data-batch-action="select-items"]').click();
    await page.locator('[data-batch-task-check="ui-task-1"]').check();
    await page.locator('[data-batch-task-check="ui-task-2"]').check();
    assert.match(await page.locator('[data-batch-selected-count]').innerText(), /2/);
    failOne = true;
    await page.locator('[data-batch-action="delete-selected-items"]').click();
    await page.waitForFunction(() => document.body.innerText.includes('远端停止尚未确认'));
    assert.deepEqual(deleteRequests[0].task_ids, ['ui-task-1', 'ui-task-2']);
    assert.equal(deleteRequests[0].cancel_active, true);
    assert.match(confirmations.at(-1), /2/);
    assert.equal(deleted.has('ui-task-1'), true);
    assert.equal(deleted.has('ui-task-2'), false, '删除失败的任务不得当作成功消失');
    assert.equal(deleted.has('ui-task-3'), false, '未勾选任务不得删除');
    await page.screenshot({ path: path.join(OUTPUT, `comfyui_delete_${viewport.width}.png`) });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, '批量管理在手机上不能横向溢出');
    await verifyErrors();

    // 同批视频切换与关闭必须暂停旧播放器；短视频由调用方提供，缺省仅执行图片回归。
    if (video) {
      mode = 'video'; deleted.clear(); failOne = false;
      await page.reload({ waitUntil: 'networkidle' }); await openBatch();
      const preview = page.locator('[data-media-preview-open]').first();
      await preview.waitFor({ state: 'visible' }); await preview.click();
      await page.locator('.comfyui-media-preview-overlay').waitFor({ state: 'visible' });
      await page.locator('.comfyui-media-preview-overlay video').evaluate(element => {
        window.__oldPreview = element; window.__pauseCalls = 0;
        const original = element.pause.bind(element);
        element.pause = () => { window.__pauseCalls += 1; return original(); };
      });
      await page.locator('[data-media-preview-step="1"]').click();
      assert.match(await page.locator('[data-media-preview-counter]').innerText(), /2\s*\/\s*3/);
      assert.equal(await page.evaluate(() => window.__pauseCalls > 0 && window.__oldPreview.paused), true);
      await page.locator('[data-media-preview-close]').click();
      await page.locator('.comfyui-media-preview-overlay').waitFor({ state: 'detached' });
    }

    // 批量分组的扩展不能破坏对话附件的原有灯箱。
    await page.evaluate(async () => {
      const studio = await import('/js/22-studio.js');
      studio.closeStudioView();
      const core = await import('/js/01-core.js');
      const chat = await import('/js/04-messages.js');
      core.state.conversationId = '';
      chat.renderMessages([{ id: 'ui-chat-images', role: 'user', content: '对话图片回归', metadata: {
        attachments: [1, 2, 3].map(index => ({ name: `chat-${index}.png`, path: `/managed/uploads/chat-${index}.png` })),
      } }]);
    });
    await page.locator('#messages img[data-large-url]').first().click();
    await page.locator('#imageLightbox').waitFor({ state: 'visible' });
    assert.equal((await page.locator('#imageLightboxCounter').innerText()).trim(), '1 / 3');
    await page.locator('#imageLightboxNext').click();
    assert.equal((await page.locator('#imageLightboxCounter').innerText()).trim(), '2 / 3');
    await page.keyboard.press('Escape');

    await page.evaluate(async () => {
      const studio = await import('/js/22-studio.js');
      studio.openStudioView(); studio.setStudioPage('labeler');
    });
    await page.locator('[data-labeler-page-tab="custom"]').click();
    const prompt = page.locator('#labelerCustomSystemPrompt');
    await prompt.waitFor({ state: 'visible' });
    await page.locator('#labelerSkillSelect option').first().waitFor({ state: 'attached' });
    await page.locator('#labelerSkillLoadFull').click();
    const deadline = Date.now() + 5000;
    while (!skillRequested && Date.now() < deadline) await page.waitForTimeout(30);
    assert.equal(skillRequested, true);
    await page.locator('#labelerSkillClear').click();
    await prompt.fill('人工写的新提示词');
    delayedSkill();
    await page.waitForTimeout(200);
    assert.equal(await prompt.inputValue(), '人工写的新提示词', '迟到Skill响应不能覆盖人工输入');
    longSkill = true;
    await page.locator('#labelerSkillLoadFull').click();
    await page.locator('#labelerSkillNote').filter({ hasText: /超过.*12000|12000.*上限/ }).waitFor({ state: 'visible' });
    assert.equal(await prompt.inputValue(), '人工写的新提示词', '超长Skill必须明确拒绝，保留原提示词');

    // 重试后的详情尚在刷新时，旧删除入口不能把条目数误当0；恢复后确认准确数量。
    mode = 'image'; retryMode = true; deleted.clear(); failOne = false;
    await openBatch();
    await page.locator('[data-batch-action="retry-item"]').first().click();
    const batchDeadline = Date.now() + 5000;
    while (!releaseBatch && Date.now() < batchDeadline) await page.waitForTimeout(30);
    assert.ok(releaseBatch, '必须真实挂起重试后的详情刷新');
    assert.equal(await page.locator('[data-batch-action="delete-batch"]').isVisible(), false, '详情加载时不保留可误点的旧删除入口');
    const resumeBatch = releaseBatch;
    releaseBatch = null;
    resumeBatch();
    await page.locator('[data-batch-action="delete-batch"]').waitFor({ state: 'visible' });
    await page.locator('[data-batch-action="delete-batch"]').click();
    await page.waitForFunction(() => document.querySelectorAll('[data-batch-open]').length === 0);
    assert.match(confirmations.at(-1), /3 个条目/);
    assert.doesNotMatch(confirmations.at(-1), /0 个条目/);
    assert.equal(batchExists, false);
    await verifyErrors();
    console.log(`通过 ${viewport.width}×${viewport.height}：灯箱切换/滑动/翻页另存为、屏外产物、批量删除/部分失败/刷新期间保护、Skill输入保护${video ? '、视频暂停' : ''}`);
  } catch (error) {
    if (delayedSkill) delayedSkill();
    if (releaseBatch) releaseBatch();
    await page.screenshot({ path: path.join(OUTPUT, `comfyui_preview_delete_failed_${viewport.width}.png`) });
    throw error;
  } finally { await page.close(); }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const viewport of [{ width: 1280, height: 900 }, { width: 375, height: 812 }]) await runViewport(browser, viewport);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
