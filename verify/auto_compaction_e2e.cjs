const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8831';
const TITLE = process.env.NAIBA_COMPACTION_TITLE || '自动整理继续本轮冒烟';
const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : ` -> ${detail}`}`);
  if (!ok) failures.push(label);
};

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !/Failed to load resource/.test(message.text())) {
      errors.push(`console.error: ${message.text()}`);
    }
  });
  try {
    await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 30000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { state: 'attached', timeout: 30000 });
    const opened = await page.evaluate((title) => {
      const item = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item')]
        .find((row) => row.querySelector('.conversation-open')?.textContent === title);
      const target = item || document.querySelector('#sidebarWorkspaceTree .conversation-item');
      target?.querySelector('.conversation-open')?.click();
      return Boolean(target && item);
    }, TITLE);
    check('找到并打开隔离会话', opened);
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.fill('#messageInput', '整理后继续本轮：请给出最终确认。');
    await page.click('#sendButton');
    await page.waitForSelector('.compaction-card', { timeout: 60000 });
    check('前端显示上下文整理卡', await page.locator('.compaction-card').count() === 1);
    await page.waitForFunction(() => document.querySelector('#messages')?.textContent.includes('正式回复：整理后继续成功。'), null, { timeout: 12000 });
    check('整理后正式回复继续完成', await page.locator('#messages').textContent().then((text) => text.includes('正式回复：整理后继续成功。')));
    const card = await page.locator('.compaction-card').textContent();
    check('整理卡含模型总结正文', String(card).includes('自动整理请求已完成'));
    check('无页面脚本错误', errors.length === 0, errors.join(' | '));
  } catch (error) {
    check('Playwright 执行未异常', false, error.message);
  } finally {
    await browser.close();
  }
  process.exit(failures.length ? 1 : 0);
})();
