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
    await page.fill('#messageInput', kind === 'first' ? '故障首轮：连续读取9份资料并完成任务。' : '故障多轮：摘要恢复后继续。');
    await page.click('#sendButton');
    await page.waitForFunction(() => document.querySelector('#messages')?.textContent.includes('故障回归：本轮已完成。'), null, { timeout: 45000 });
    await page.waitForFunction(async () => !(await import('/js/01-core.js')).state.chatBusy);
    await page.locator('.compaction-card').first().waitFor();
    await check();
    const state = await (await page.request.get(`${BASE}/_fixture/state`)).json();
    assert.ok(state.summaries >= 2, '第一次 length 后确有恢复请求');
    if (kind === 'first') assert.equal(state.turn, 10, '9 次工具调用和最后回复各执行一次');
    await page.reload({ waitUntil: 'load' });
    await page.evaluate(async () => { await import(document.querySelector('script[src*="/js/main.js"]').src); });
    await page.waitForFunction(async () => Boolean((await import('/js/01-core.js')).state.bootstrap));
    await page.evaluate(async id => (await import('/js/08-conversations.js')).openConversation(id), conversations[scenario]);
    await page.locator('.compaction-card').first().waitFor();
    await check();
    if (kind === 'first') {
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
    console.log(`PASS ${scenario}: 截断恢复、完整记录、刷新/下一轮/撤销，无脚本错误`);
  } finally { await page.close(); }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try { for (const width of [1280, 375]) for (const kind of ['history', 'first']) await run(browser, width, kind); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
