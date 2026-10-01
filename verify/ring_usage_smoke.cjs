// 上下文圆环逐请求刷新 + 达阈值弹窗提醒一次（由 verify/ring_usage_smoke.py 编排）。
// 断言：run 仍在进行中时圆环就按 usage 事件刷新；未达阈值不弹窗；达阈值弹一次并带阈值；
// 同一会话内再次升高不再弹第二次。
const { chromium } = require('playwright');
const fs = require('fs');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8797';
const TITLE = process.env.NAIBA_RING_TITLE || '圆环用量冒烟';
const STEP = process.env.NAIBA_RING_STEP || '';
const failures = [];

function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function waitFor(page, fn, timeout = 20000, interval = 250) {
  const deadline = Date.now() + timeout;
  let last = null;
  while (Date.now() < deadline) {
    last = await fn();
    if (last) return last;
    await page.waitForTimeout(interval);
  }
  return last;
}

const text = (page, selector) => page.evaluate(
  (sel) => document.querySelector(sel)?.textContent || '', selector);
const dialogOpen = (page) => page.evaluate(
  () => Boolean(document.querySelector('#contextWarningDialog')?.open));
const percent = (page) => page.evaluate(
  () => document.querySelector('#contextUsageRing')?.style.getPropertyValue('--context-percent') || '');
// 「思考 自动」右侧的实时百分比标签（文本 + 配色类 + 相对思考标签的位置）。
const inlinePercent = (page) => page.evaluate(() => {
  const label = document.querySelector('#contextPercentLabel');
  const reasoning = document.querySelector('#reasoningLabel');
  if (!label) return null;
  const rect = label.getBoundingClientRect();
  const reasonRect = reasoning ? reasoning.getBoundingClientRect() : { right: 0 };
  return {
    text: label.textContent.trim(),
    className: label.className,
    title: label.title,
    rightOfReasoning: rect.left >= reasonRect.right - 1,
    visible: rect.width > 0 && rect.height > 0,
  };
});
const signal = (step) => { if (STEP) fs.writeFileSync(STEP, String(step)); };

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  // 首启引导弹窗（`maybeShowOnboarding`：没有**可用在线供应商**时自动 showModal）会盖住
  // 侧栏并拦截点击。本冒烟只播种了本地模型档（model_profiles），在没配在线供应商的机器/
  // 隔离库上必然弹窗，于是"点开会话"稳定超时——与冒烟要考的圆环无关。与其它冒烟同口径，
  // 先标记已忽略（只影响这个临时浏览器上下文，不落任何用户数据）。
  await page.addInitScript(() => {
    try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) { /* 无存储权限 */ }
  });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${TITLE}") .conversation-open`);
    await page.waitForSelector('#messages .usage-stream', { timeout: 20000 });

    // ---- 1) usage #1（8,200 = 8.2%，阈值 50%）----
    await waitFor(page, async () => (await text(page, '#contextUsageSummary')).includes('8,200'));
    const summary1 = await text(page, '#contextUsageSummary');
    const status1 = await text(page, '#runtimeStatus');
    check('圆环按 usage 事件刷新（未等整轮结束）', summary1.includes('8,200'), summary1);
    check('圆环渲染百分比', summary1.includes('8.2%'), summary1);
    const inline1 = await inlinePercent(page);
    check('「思考 自动」右侧显示实时百分比', Boolean(inline1) && inline1.text === '上下文 8.2%',
      JSON.stringify(inline1));
    check('百分比排在「思考 自动」右侧且可见',
      Boolean(inline1) && inline1.rightOfReasoning && inline1.visible, JSON.stringify(inline1));
    check('未达 70% 不显示告警配色',
      Boolean(inline1) && !inline1.className.includes('warning') && !inline1.className.includes('danger'),
      JSON.stringify(inline1));
    check('悬停提示带完整用量', Boolean(inline1) && inline1.title.includes('8,200'), JSON.stringify(inline1));
    check('断言时该轮仍在进行中', status1 !== '就绪', `runtimeStatus=${status1}`);
    check('未达阈值不弹窗', !(await dialogOpen(page)));

    // ---- 2) usage #2（85,000 = 85%）----
    signal(2);
    await waitFor(page, async () => (await text(page, '#contextUsageSummary')).includes('85,000'), 30000);
    const percent2 = await percent(page);
    check('第二次请求再次刷新圆环', (await text(page, '#contextUsageSummary')).includes('85.0%'));
    const inline2 = await inlinePercent(page);
    check('百分比随第二次请求实时刷新', Boolean(inline2) && inline2.text === '上下文 85.0%',
      JSON.stringify(inline2));
    check('达 70% 转为告警配色', Boolean(inline2) && inline2.className.includes('warning'),
      JSON.stringify(inline2));
    const opened = await waitFor(page, async () => (await dialogOpen(page)), 15000);
    check('达到阈值弹出提醒', Boolean(opened));
    const detail = await text(page, '#contextWarningDetail');
    check('弹窗文案含当前用量与阈值', detail.includes('85.0%') && detail.includes('50%'), detail);

    // 外观：紧凑 + 居中 + 按钮居中 + 正文文案
    const box = await page.evaluate(() => {
      const dialog = document.querySelector('#contextWarningDialog');
      const rect = dialog.getBoundingClientRect();
      // 只量"可见"的按钮（发送模式下会多出隐藏的「继续发送」）
      const visible = [...dialog.querySelectorAll('.form-actions button')]
        .find((el) => !el.hidden && el.getBoundingClientRect().width > 0);
      const button = visible ? visible.getBoundingClientRect() : { left: 0, width: 0 };
      return {
        height: rect.height,
        centerX: rect.left + rect.width / 2,
        centerY: rect.top + rect.height / 2,
        buttonCenterX: button.left + button.width / 2,
        buttonLabel: visible ? visible.textContent.trim() : '',
        dialogCenterX: rect.left + rect.width / 2,
        viewportW: window.innerWidth,
        viewportH: window.innerHeight,
        body: dialog.querySelector('.context-warning-body')?.textContent || '',
      };
    });
    check('弹窗水平居中', Math.abs(box.centerX - box.viewportW / 2) <= 4, JSON.stringify(box));
    check('弹窗垂直居中', Math.abs(box.centerY - box.viewportH / 2) <= 4, JSON.stringify(box));
    check('弹窗紧凑（高度 ≤ 240px）', box.height <= 240, String(box.height));
    check('可见按钮水平居中', Math.abs(box.buttonCenterX - box.dialogCenterX) <= 4,
      `${box.buttonCenterX} vs ${box.dialogCenterX}`);
    check('运行中只显示「知道了」', box.buttonLabel === '知道了', box.buttonLabel);
    check('正文文案已更新', box.body.includes('撰写交接文档'), box.body);
    await page.screenshot({ path: `${__dirname}\\ring_warning_dialog.png` });

    // 关闭弹窗（走真实关闭路径）
    await page.click('#contextWarningDialog [data-close="contextWarningDialog"]');
    await waitFor(page, async () => !(await dialogOpen(page)), 8000);
    check('弹窗可关闭', !(await dialogOpen(page)));

    // ---- 3) usage #3（88,000 = 88%）：只涨 3% → 不弹 ----
    signal(3);
    await waitFor(page, async () => (await text(page, '#contextUsageSummary')).includes('88,000'), 30000);
    await page.waitForTimeout(2500);
    check('距上次提醒不足 5% 不再弹', !(await dialogOpen(page)));

    // ---- 3b) usage #4（91,000 = 91%）：再涨 6% → 再次弹 ----
    signal(4);
    await waitFor(page, async () => (await text(page, '#contextUsageSummary')).includes('91,000'), 30000);
    const reopened = await waitFor(page, async () => (await dialogOpen(page)), 15000);
    check('再涨 5% 以上重新弹窗', Boolean(reopened));
    check('百分比继续变大', parseFloat(await percent(page)) > parseFloat(percent2 || '0'),
      `${percent2} -> ${await percent(page)}`);
    const inline4 = await inlinePercent(page);
    check('达 90% 转为危险配色', Boolean(inline4) && inline4.className.includes('danger'),
      JSON.stringify(inline4));
    await page.click('#contextWarningDialog [data-close="contextWarningDialog"]');
    await waitFor(page, async () => !(await dialogOpen(page)), 8000);

    // ---- 4) 已结束且超限的会话：打开不弹，点发送才弹 ----
    const IDLE_TITLE = process.env.NAIBA_RING_IDLE_TITLE || '圆环空闲冒烟';
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${IDLE_TITLE}") .conversation-open`);
    await waitFor(page, async () => (await text(page, '#contextUsageSummary')).includes('88,000'), 20000);
    await page.waitForTimeout(2500);
    check('打开已结束的超限会话不弹窗', !(await dialogOpen(page)));
    await page.fill('#messageInput', '继续这个会话');
    await page.click('#sendButton');
    const sentWarning = await waitFor(page, async () => (await dialogOpen(page)), 8000);
    check('点发送才弹窗', Boolean(sentWarning));
    check('「继续发送」按钮在发送模式可见',
      await page.evaluate(() => {
        const button = document.querySelector('#contextWarningContinue');
        return Boolean(button && !button.hidden);
      }));
    check('被拦下时草稿未被清空',
      (await page.inputValue('#messageInput')) === '继续这个会话',
      await page.inputValue('#messageInput'));
    const before = await page.evaluate(() => document.querySelectorAll('#messages .message-row.user').length);
    await page.click('#contextWarningContinue');
    await waitFor(page, async () => !(await dialogOpen(page)), 8000);
    const after = await waitFor(page, async () => {
      const count = await page.evaluate(() => document.querySelectorAll('#messages .message-row.user').length);
      return count > before ? count : 0;
    }, 8000);
    check('「继续发送」后消息真的发出', Boolean(after), `before=${before} after=${after}`);
    check('零页面错误', pageErrors.length === 0, pageErrors.join(' | '));

    await page.screenshot({ path: `${__dirname}\\ring_usage_smoke.png` });
  } catch (error) {
    failures.push(`执行异常: ${error.message}`);
    console.log('执行异常:', error.message);
  }

  await browser.close();
  console.log('');
  console.log('圆环/弹窗冒烟结果：', failures.length ? `${failures.length} 项失败 -> ${JSON.stringify(failures)}` : '全部通过');
  process.exit(failures.length ? 1 : 0);
})();
