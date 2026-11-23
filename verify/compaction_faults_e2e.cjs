const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const BASE = process.env.NAIBA_SMOKE_BASE;
const conversations = JSON.parse(process.env.NAIBA_COMPACTION_CONVERSATIONS);
const output = process.env.NAIBA_SMOKE_OUTPUT;

async function run(browser, width, kind) {
  const scenario = `${kind}-${width}`;
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !/Failed to load resource/.test(message.text())) errors.push(message.text());
  });
  page.on('response', response => {
    if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) errors.push(`${response.status()} ${response.url()}`);
  });
  const check = async () => {
    assert.deepEqual(errors, []);
    assert.equal(await page.locator('body').evaluate(el => /JS ERROR|PROMISE REJECT/.test(el.innerText)), false);
  };
  try {
    await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
    await page.request.post(`${BASE}/_fixture/scenario`, { data: { scenario } });
    await page.goto(BASE, { waitUntil: 'load' });
    await page.evaluate(async () => { await import(document.querySelector('script[src*="/js/main.js"]').src); });
    await page.waitForFunction(async () => Boolean((await import('/js/01-core.js')).state.bootstrap));
    await page.evaluate(async id => (await import('/js/08-conversations.js')).openConversation(id), conversations[scenario]);
    await check();
    await page.clock.install();
    await page.evaluate(() => {
      window.compactionToasts = [];
      new MutationObserver(() => window.compactionToasts.push(document.querySelector('#toast').textContent))
        .observe(document.querySelector('#toast'), { childList: true, subtree: true });
    });
    const toolTurn = kind === 'first' || kind === 'second';
    await page.fill('#messageInput', toolTurn ? `故障${kind === 'first' ? '首' : '第二'}轮：连续读取9份资料并完成任务。` : '故障多轮：摘要恢复后继续。');
    await page.click('#sendButton');
    const status = page.locator('#runtimeStatus');
    await page.waitForFunction(() => document.querySelector('#runtimeStatus')?.classList.contains('compaction-running'));
    if (kind === 'first') {
      await page.evaluate(async () => {
        const state = (await import('/js/01-core.js')).state;
        await (await import('/js/11-run-stream.js')).resumeRun({ id: state.chatRunId, conversation_id: state.conversationId });
      });
      await page.waitForFunction(async () => document.querySelector('#runtimeStatus')?.classList.contains('compaction-running')
        && (await import('/js/01-core.js')).state.elapsedBase === '正在整理上下文'
        && Boolean((await import('/js/01-core.js')).state.runWatchdogTimer));
    }
    if (kind === 'second') {
      await page.evaluate(async id => (await import('/js/08-conversations.js')).openConversation(id), conversations[`history-${width}`]);
      await page.clock.fastForward(1100);
      assert.equal(await page.locator('.compaction-running').count(), 0, '切到其他会话后清除整理指示');
      assert.equal((await status.textContent()).includes('整理'), false, '旧整理计时不能覆盖其他会话状态');
      await page.evaluate(async id => (await import('/js/08-conversations.js')).openConversation(id), conversations[scenario]);
      await page.waitForFunction(() => document.querySelector('#runtimeStatus')?.classList.contains('compaction-running'));
    }
    if (kind === 'first') await page.waitForTimeout(1200);
    else await page.clock.fastForward(7100);
    assert.equal(await status.isVisible(), true, '摘要等待期间整理提醒可见');
    const statusText = await status.textContent();
    assert.match(statusText, /正在整理上下文.*已等待 \d+ 秒/, '整理提醒持续接管等待计时');
    const elapsedSeconds = Number(statusText.match(/已等待 (\d+) 秒/)[1]);
    assert.ok(elapsedSeconds >= (kind === 'first' ? 1 : 7), `整理计时继续前进 ${statusText}`);
    const geometry = await status.evaluate(element => {
      const bounds = element.getBoundingClientRect();
      const container = element.closest('.chat-main').getBoundingClientRect();
      return { left: bounds.left, right: bounds.right, containerLeft: container.left, containerRight: container.right,
        width: bounds.width, height: bounds.height, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth };
    });
    assert.ok(geometry.left >= geometry.containerLeft - 1 && geometry.right <= geometry.containerRight + 1,
      `整理提醒位于聊天区域内 ${JSON.stringify(geometry)}`);
    assert.ok(geometry.width > 0 && geometry.height > 0, '整理提醒有可见尺寸');
    assert.ok(geometry.scrollWidth <= geometry.clientWidth + 1, '整理提醒文字完整显示');
    await page.screenshot({ path: path.join(output, `notice-${scenario}.png`) });
    await page.request.post(`${BASE}/_fixture/release-summary`, { data: {} });
    await page.waitForFunction(() => document.querySelector('#messages')?.textContent.includes('故障回归：本轮已完成。'), null, { timeout: 45000 });
    await page.waitForFunction(async () => !(await import('/js/01-core.js')).state.chatBusy);
    await page.locator('.compaction-card').first().waitFor();
    assert.equal(await page.locator('.compaction-running').count(), 0, '整理完成后清除运行指示');
    assert.equal(await page.evaluate(() => window.compactionToasts.some(text => /整理成摘要|已整理较早/.test(text))), true,
      '整理完成后收到成功提示');
    await check();
    const state = await (await page.request.get(`${BASE}/_fixture/state`)).json();
    assert.ok(state.summaries >= 2, '第一次 length 后确有恢复请求');
    if (toolTurn) assert.equal(state.turn, 10, '9 次工具调用和最后回复各执行一次');
    await page.reload({ waitUntil: 'load' });
    await page.evaluate(async () => { await import(document.querySelector('script[src*="/js/main.js"]').src); });
    await page.waitForFunction(async () => Boolean((await import('/js/01-core.js')).state.bootstrap));
    await page.evaluate(async id => (await import('/js/08-conversations.js')).openConversation(id), conversations[scenario]);
    await page.locator('.compaction-card').first().waitFor();
    await check();
    if (toolTurn) {
      await page.fill('#messageInput', '下一轮：确认整理后仍可继续');
      await page.click('#sendButton');
      await page.waitForFunction(() => document.querySelector('#messages')?.textContent.includes('下一轮使用已整理视图成功。'), null, { timeout: 30000 });
      await page.waitForFunction(async () => !(await import('/js/01-core.js')).state.chatBusy);
      await check();
      page.on('dialog', dialog => dialog.accept());
      const card = page.locator('.compaction-card').first();
      await card.locator('summary').click();
      await card.locator('[data-cancel-compaction]').click();
      await page.waitForFunction(() => !document.querySelector('.compaction-card'));
      await check();
    }
    await page.screenshot({ path: path.join(output, `faults-${scenario}.png`) });
    console.log(`PASS ${scenario}: 整理中持续提醒、重连/切换恢复、成功提示、截断恢复、刷新/下一轮/撤销，无脚本错误`);
  } finally { await page.close(); }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try { for (const width of [1280, 375]) for (const kind of ['history', 'first', 'second']) await run(browser, width, kind); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
