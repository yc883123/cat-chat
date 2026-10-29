// 左侧会话活跃状态图标冒烟（不创建真实 Run）。
// 运行：$env:NODE_PATH="$env:USERPROFILE\node_modules"; node verify\sidebar_run_status_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  let activeConversationId = '';
  let runActive = true;
  page.on('pageerror', (error) => pageErrors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => { if (message.type() === 'error') pageErrors.push(`console.error: ${message.text()}`); });
  await page.route('**/api/runs?active_only=1', async (route) => {
    const runs = runActive && activeConversationId
      ? [{ id: 'sidebar-status-smoke-run', conversation_id: activeConversationId, kind: 'chat', status: 'running' }]
      : [];
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ runs }) });
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { state: 'attached', timeout: 20000 });
    const visible = await page.locator('#sidebarWorkspaceTree .conversation-item').evaluateAll((items) => items.map((item) => ({
      id: item.dataset.conversationId,
      active: item.classList.contains('active'),
    })));
    const nonCurrent = visible.find((item) => !item.active) || visible[0];
    activeConversationId = nonCurrent?.id || '';
    check('侧栏存在可用于检查的会话', Boolean(activeConversationId), JSON.stringify(visible));

    for (const viewport of [{ width: 1280, height: 900, label: '桌面' }, { width: 375, height: 812, label: '手机' }]) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      runActive = true;
      await page.reload({ waitUntil: 'load' });
      await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { state: 'attached', timeout: 20000 });
      await page.waitForFunction((id) => {
        const item = document.querySelector(`.conversation-item[data-conversation-id="${CSS.escape(id)}"]`);
        return item?.querySelector('.conversation-run-status');
      }, activeConversationId, { timeout: 15000 });
      const activeState = await page.evaluate((id) => {
        const item = document.querySelector(`.conversation-item[data-conversation-id="${CSS.escape(id)}"]`);
        const icon = item?.querySelector('.conversation-run-status');
        const title = item?.querySelector('.conversation-open');
        return {
          icon: Boolean(icon),
          accessibleName: icon?.getAttribute('aria-label') || '',
          visibleText: item?.textContent || '',
          nonCurrent: !item?.classList.contains('active'),
          titleRect: title?.getBoundingClientRect().toJSON(),
          rowRect: item?.getBoundingClientRect().toJSON(),
          animation: icon ? getComputedStyle(icon.querySelector('svg')).animationName : '',
        };
      }, activeConversationId);
      check(`${viewport.label}活跃会话显示纯图标`, activeState.icon && !activeState.visibleText.includes('正在回复'), JSON.stringify(activeState));
      check(`${viewport.label}非当前会话状态可见`, activeState.nonCurrent || visible.length === 1, JSON.stringify(activeState));
      check(`${viewport.label}图标动画已启用`, activeState.animation === 'conversation-run-spin', JSON.stringify(activeState));
      check(`${viewport.label}标题未溢出会话行`, Boolean(activeState.titleRect && activeState.rowRect
        && activeState.titleRect.left >= activeState.rowRect.left
        && activeState.titleRect.right <= activeState.rowRect.right), JSON.stringify(activeState));

      runActive = false;
      await page.reload({ waitUntil: 'load' });
      await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { state: 'attached', timeout: 20000 });
      await page.waitForTimeout(1800);
      const iconRemains = await page.evaluate((id) => Boolean(document.querySelector(
        `.conversation-item[data-conversation-id="${CSS.escape(id)}"] .conversation-run-status`,
      )), activeConversationId);
      check(`${viewport.label}Run 结束后图标消失`, !iconRemains);
    }
    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 5).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error?.stack || error));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`左侧活跃会话状态图标冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
