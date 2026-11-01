// 任务总管控制面浏览器冒烟：验证创建弹窗的桌面/移动布局与可见错误提示。
// 由外部隔离源码实例提供页面；使用系统 Edge，避免误验打包版资源。
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8798';
const SHOTS_DIR = process.env.NAIBA_SMOKE_SHOTS || path.join(__dirname, 'super_agent_controls_shots');
const failures = [];

function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${!ok && detail ? `  -> ${detail}` : ''}`);
  if (!ok) failures.push(label);
}

async function box(page, selector) {
  return page.$eval(selector, (node) => {
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return {
      left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
      width: rect.width, height: rect.height, display: style.display,
      gap: style.gap, overflowY: style.overflowY,
    };
  });
}

(async () => {
  fs.mkdirSync(SHOTS_DIR, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const viewport of [{ name: 'desktop', width: 1280, height: 900 }, { name: 'mobile', width: 430, height: 900 }]) {
      const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } });
      const pageErrors = [];
      const consoleErrors = [];
      const badResponses = [];
      page.on('pageerror', (error) => pageErrors.push(String(error?.message || error)));
      page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()); });
      page.on('response', (response) => {
        if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) badResponses.push(`${response.status()} ${response.url()}`);
      });
      await page.route('**/favicon.ico', (route) => route.fulfill({ status: 204, body: '' }));
      await page.route('**/api/providers/models', (route) => route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ models: [] }),
      }));

      try {
        await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
        await page.waitForSelector('#openSuperAgent', { state: 'visible', timeout: 15000 });
        // 首启引导在启动初始化后异步出现，先给它一个稳定窗口再关闭，避免遮挡入口点击。
        await page.waitForTimeout(1200);
        const onboarding = page.locator('#onboardingDialog[open]');
        if (await onboarding.count()) {
          await page.locator('#onboardingSkip').click({ timeout: 5000 });
          await page.waitForFunction(() => !document.querySelector('#onboardingDialog')?.open, null, { timeout: 5000 });
        }
        if (viewport.name === 'mobile') {
          await page.locator('#openSidebar').click({ timeout: 5000 });
          await page.waitForTimeout(150);
        }
        await page.locator('#openSuperAgent').click({ timeout: 5000 });
        await page.waitForSelector('#superAgentView', { state: 'visible', timeout: 15000 });
        await page.locator('#superAgentCreate').click({ timeout: 5000 });
        await page.waitForSelector('#superAgentCreateDialog[open]', { state: 'visible', timeout: 5000 });

        const dialog = await box(page, '#superAgentCreateDialog');
        const form = await box(page, '#superAgentCreateForm');
        const head = await box(page, '#superAgentCreateForm .writer-manage-head');
        const title = await box(page, '#superAgentCreateTitle');
        const close = await box(page, '#superAgentCreateClose');
        const actions = await box(page, '#superAgentCreateForm .writer-dialog-actions');
        const geometry = await page.evaluate(() => ({
          viewport: { width: innerWidth, height: innerHeight },
          document: { scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth },
          dialogOpen: document.querySelector('#superAgentCreateDialog')?.open,
          mobileDialog: getComputedStyle(document.querySelector('#superAgentCreateDialog')).height,
        }));

        check(`${viewport.name}: 任务总管视图可见`, await page.locator('#superAgentView').isVisible());
        check(`${viewport.name}: 创建弹窗标题与关闭按钮同一横向标题区`,
          head.top <= title.top + 1 && head.top <= close.top + 1 && head.bottom >= title.bottom - 1 && head.bottom >= close.bottom - 1,
          JSON.stringify({ head, title, close }));
        check(`${viewport.name}: 标题和关闭按钮不横向重叠`, title.right <= close.left || close.right <= title.left,
          JSON.stringify({ title, close }));
        check(`${viewport.name}: 表单按钮区仍在弹窗内`, actions.left >= dialog.left - 1 && actions.right <= dialog.right + 1 && actions.bottom <= dialog.bottom + 1,
          JSON.stringify({ dialog, actions }));
        check(`${viewport.name}: 表单内容可滚动且控件不越界`, form.width <= dialog.width + 1 && form.overflowY !== 'visible',
          JSON.stringify({ dialog, form }));

        await page.click('#superAgentCreateSubmit');
        const errorText = (await page.textContent('#superAgentCreateError') || '').trim();
        check(`${viewport.name}: 空表单错误提示可见`, errorText.includes('请选择一个可用模型'), errorText);
        const error = await box(page, '#superAgentCreateError');
        check(`${viewport.name}: 错误提示位于按钮区之前`, error.bottom <= actions.top + 1, JSON.stringify({ error, actions }));

        if (viewport.name === 'mobile') {
          check('mobile: 创建弹窗使用全屏高度', Math.abs(dialog.width - viewport.width) <= 1 && Math.abs(dialog.height - viewport.height) <= 1,
            JSON.stringify({ dialog, viewport, geometry }));
        }
        check(`${viewport.name}: 无横向溢出`, geometry.document.scrollWidth <= geometry.document.clientWidth + 1, JSON.stringify(geometry.document));
        await page.screenshot({ path: path.join(SHOTS_DIR, `super-agent-create-${viewport.name}.png`), fullPage: false });
      } catch (error) {
        check(`${viewport.name}: 冒烟执行`, false, String(error?.message || error).split('\n')[0]);
      } finally {
        check(`${viewport.name}: 零 pageerror`, pageErrors.length === 0, pageErrors.join(' | '));
        check(`${viewport.name}: 零 console.error`, consoleErrors.length === 0, consoleErrors.join(' | '));
        check(`${viewport.name}: 无 4xx/5xx 响应`, badResponses.length === 0, badResponses.join(' | '));
        await page.close();
      }
    }
  } finally {
    await browser.close();
  }
  console.log(`\n结果：${failures.length ? `失败 ${failures.length} 项：${failures.join(' / ')}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
