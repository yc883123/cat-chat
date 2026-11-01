const { chromium } = require('playwright');

const baseURL = process.env.CAT_CHAT_URL || 'http://127.0.0.1:8765';
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const viewport of [{ width: 1280, height: 800 }, { width: 375, height: 812 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [];
      page.on('pageerror', (error) => errors.push(String(error)));
      page.on('response', (response) => { if (response.status() >= 500) errors.push(`HTTP ${response.status()} ${response.url()}`); });
      await page.goto(baseURL, { waitUntil: 'domcontentloaded', timeout: 15000 });
      await page.waitForTimeout(500);
      await page.locator('#openSuperAgent').click();
      await page.waitForTimeout(400);
      if (!(await page.locator('#superAgentView').isVisible())) throw new Error('任务总管视图未显示');
      const width = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
      if (width.scrollWidth > width.clientWidth + 1) throw new Error(`横向溢出 ${JSON.stringify(width)}`);
      await page.locator('#openChatMode').click();
      if (!(await page.locator('#messages').isVisible())) throw new Error('Chat 未恢复');
      if (errors.length) throw new Error(errors.join('; '));
      await page.close();
    }
  } finally { await browser.close(); }
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
