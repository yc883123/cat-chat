const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8831';
const conversations = JSON.parse(process.env.NAIBA_COMPACTION_CONVERSATIONS || '{}');
const output = process.env.NAIBA_SMOKE_OUTPUT || __dirname;
const answer = '正式回复：整理后继续成功。';

async function run(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  const errors = [];
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !/Failed to load resource/.test(message.text())) {
      errors.push(`console.error: ${message.text()}`);
    }
  });
  page.on('response', response => {
    if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) {
      errors.push(`HTTP ${response.status()} ${new URL(response.url()).pathname}`);
    }
  });
  const check = async () => {
    assert.deepEqual(errors, [], '无非预期接口或脚本错误');
    assert.equal(await page.locator('body').evaluate(el => /JS ERROR|PROMISE REJECT/.test(el.innerText)), false);
  };
  const snapshot = () => page.evaluate(() => ({
    usage: { ...window.compactionState.contextUsage },
    label: document.querySelector('#contextPercentLabel')?.textContent || '',
  }));
  try {
    await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 30000 });
    await page.evaluate(async () => {
      await import(document.querySelector('script[src*="/js/main.js"]').src);
      window.compactionState = (await import('/js/01-core.js')).state;
    });
    await page.waitForFunction(() => Boolean(window.compactionState.updatePollTimer));
    assert.ok(conversations[String(width)], '隔离会话已播种');
    await page.evaluate(async id => {
      await (await import('/js/08-conversations.js')).openConversation(id);
    }, conversations[String(width)]);
    await page.waitForFunction(() => window.compactionState.contextUsage?.context_tokens === 24200);
    const before = await snapshot();
    await check();
    assert.equal((await page.request.post(`${BASE}/_fixture/hold-answer`, { data: {} })).status(), 200);
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.fill('#messageInput', '整理后继续本轮：请给出最终确认。');
    await page.click('#sendButton');
    await page.waitForFunction(() => window.compactionState.contextUsage?.context_estimated === true, null, { timeout: 20000 });
    const estimated = await snapshot();
    assert.ok(estimated.usage.context_tokens > 0 && estimated.usage.context_tokens < before.usage.context_tokens);
    const percent = label => Number(label.match(/([\d.]+)%/)?.[1]);
    assert.ok(percent(estimated.label) < percent(before.label), '正式回复用量到达前圆环已下降');
    assert.ok(estimated.label.includes('估算'), '估算值明确标识');
    for (const field of ['input_tokens', 'output_tokens', 'total_tokens']) {
      assert.equal(estimated.usage[field], before.usage[field], `估算不改写实际计费用量 ${field}`);
    }
    assert.equal((await page.locator('#messages').textContent()).includes(answer), false);
    await check();
    await page.screenshot({ path: path.join(output, `compaction-estimated-${width}.png`) });
    assert.equal((await page.request.post(`${BASE}/_fixture/release-answer`, { data: {} })).status(), 200);
    await page.waitForFunction(text => document.querySelector('#messages')?.textContent.includes(text), answer, { timeout: 20000 });
    await page.waitForFunction(() => window.compactionState.contextUsage?.context_tokens === 5640
      && !window.compactionState.contextUsage?.context_estimated);
    await page.locator('.compaction-card').waitFor({ state: 'visible', timeout: 12000 });
    assert.equal(await page.locator('.compaction-card').count(), 1, '每轮一张整理卡');
    assert.ok((await page.locator('.compaction-card').textContent()).includes('自动整理请求已完成'));
    const actual = await snapshot();
    assert.equal(actual.label.includes('估算'), false, '真实用量替换估算');
    await check();
    await page.screenshot({ path: path.join(output, `compaction-actual-${width}.png`) });
    console.log(`PASS ${width}px: 整理前 ${before.label} → 正式用量前 ${estimated.label} → 真实用量 ${actual.label}；本轮继续完成`);
  } catch (error) {
    console.error('Compaction failure', JSON.stringify({ width, errors, state: await snapshot().catch(() => null) }));
    await page.screenshot({ path: path.join(output, `compaction-failed-${width}.png`) });
    throw error;
  } finally {
    await page.request.post(`${BASE}/_fixture/release-answer`, { data: {} }).catch(() => {});
    await page.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try { for (const width of [1280, 375]) await run(browser, width); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
