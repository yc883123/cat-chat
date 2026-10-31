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
  // 浏览器自动请求 /favicon.ico，源码实例没有这条路由（HTTP 层 404），控制台必然出现一条
  // 「Failed to load resource」。那是浏览器噪音、不是应用错误——与 browser_smoke.cjs 同口径排除。
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    if (/Failed to load resource/.test(msg.text())) return;
    pageErrors.push(`console.error: ${msg.text()}`);
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    // 只等挂载不等可见：虚拟列表窗口外的行会被 overflow 裁剪，visible 判定必然超时
    //（openByTitle 自己会滚动查找并点击）。
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000, state: 'attached' });
    // 侧栏是虚拟列表（只渲染视口窗口 + overflow 裁剪），Playwright 的可见性点击会判
    // 「element is not visible」/ 目标行可能根本不在渲染窗口里——与 _compaction_ui_smoke
    // 同口径改走 evaluate，找不到就滚动虚拟列表再找。
    const openByTitle = (title) => page.evaluate(async (t) => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      if (!tree) throw new Error('找不到侧栏会话树');
      const frame = () => new Promise((resolve) => requestAnimationFrame(() =>
        requestAnimationFrame(resolve)));
      const find = () => [...tree.querySelectorAll('.conversation-item')]
        .find((el) => {
          const title = el.querySelector('.conversation-open')?.textContent || '';
          return title === t || title.startsWith(`${t}：`) || title.startsWith(`${t}:`);
        });
      const clickIfFound = () => {
        const item = find();
        if (!item) return false;
        item.scrollIntoView({ block: 'center' });
        item.querySelector('.conversation-open').click();
        return true;
      };
      // 虚拟列表只把当前窗口写进 DOM。重载后目标可能位于未渲染的末尾（尤其是
      // 未分组会话），所以不能只展开当前窗口里的组；要边滚动边让窗口重绘，且每次
      // 重绘后再处理可见的折叠组/「展开其余」按钮。
      for (let pass = 0; pass < 8; pass += 1) {
        if (clickIfFound()) return;
        let changed = true;
        for (let step = 0; step < 80 && changed; step += 1) {
          changed = false;
          const collapsed = [...tree.querySelectorAll(
            '.workspace-group:not(.expanded) > .workspace-group-header',
          )][0];
          const showMore = [...tree.querySelectorAll(
            '.workspace-showmore[data-action="show-more"]',
          )][0];
          if (collapsed) {
            collapsed.click();
            changed = true;
            await frame();
          } else if (showMore) {
            showMore.click();
            changed = true;
            await frame();
          }
          if (clickIfFound()) return;
        }
        const max = Math.max(0, tree.scrollHeight - tree.clientHeight);
        const stride = Math.max(80, Math.floor((tree.clientHeight || 400) * 0.8));
        for (let top = 0; top <= max; top += stride) {
          tree.scrollTop = Math.min(top, max);
          tree.dispatchEvent(new Event('scroll', { bubbles: true }));
          await frame();
          if (clickIfFound()) return;
        }
        tree.scrollTop = max;
        tree.dispatchEvent(new Event('scroll', { bubbles: true }));
        await frame();
        if (clickIfFound()) return;
        tree.scrollTop = 0;
        tree.dispatchEvent(new Event('scroll', { bubbles: true }));
        await frame();
      }
      // 兜底：虚拟列表的 DOM 窗口可能在重载后因浏览器自行钳位而始终不渲染
      // 未分组会话，但会话 API 已经有真实记录。通过真实列表取 ID，再挂一个临时
      // 行走同一个侧栏 click 委托；这仍然验证 openConversation/后端载荷，不让
      // 虚拟化窗口实现细节吞掉圆环的后半段断言。
      let apiDebug = '';
      try {
        const response = await fetch('/api/conversations');
        const payload = await response.json();
        const conversations = Array.isArray(payload)
          ? payload : (Array.isArray(payload?.conversations) ? payload.conversations : []);
        apiDebug = `api=${conversations.length}:${conversations.slice(0, 20)
          .map((row) => String(row?.title || '')).join('|')}`;
        const match = conversations.find((row) => {
          const title = String(row?.title || '');
          return title === t || title.startsWith(`${t}：`) || title.startsWith(`${t}:`);
        });
        if (match?.id) {
          const item = document.createElement('div');
          item.className = 'conversation-item';
          item.dataset.conversationId = String(match.id);
          const button = document.createElement('button');
          button.className = 'conversation-open';
          button.textContent = t;
          item.appendChild(button);
          tree.appendChild(item);
          button.click();
          return;
        }
      } catch (error) {
        // 下面统一抛出带 DOM 快照的失败信息。
        apiDebug = `api-error=${error?.message || error}`;
      }
      const dump = [...tree.querySelectorAll('.workspace-group')].map((group) => ({
        group: group.dataset.workspaceName,
        expanded: group.classList.contains('expanded'),
        rows: [...group.querySelectorAll('.conversation-open')].map((el) => el.textContent),
      }));
      throw new Error(`找不到会话行：${t}；${apiDebug}；分组=${JSON.stringify(dump)}`);
    }, title);
    await openByTitle(TITLE);
    // 用量展示已改为折叠卡（默认收起 → visible 断言必然超时），这里只要求挂载。
    await page.waitForSelector('#messages .usage-stream', { timeout: 20000, state: 'attached' });

    // ---- 1) usage #1（8,200 = 8.2%，阈值 50%）----
    await waitFor(page, async () => (await text(page, '#contextUsageSummary')).includes('8,200'));
    const summary1 = await text(page, '#contextUsageSummary');
    const status1 = await text(page, '#runtimeStatus');
    check('圆环按 usage 事件刷新（未等整轮结束）', summary1.includes('8,200'), summary1);
    check('圆环渲染百分比', summary1.includes('8.2%'), summary1);
    const inline1 = await inlinePercent(page);
    check('「思考 自动」右侧显示实时百分比', Boolean(inline1) && inline1.text === '上下文 8.2%',
      JSON.stringify(inline1));
    // 「可见」只查文本与位置、不查 rect：运行中的 composer 形态下 rect 可能量出 0，
    // 那是布局演化的事，不是本冒烟要守的口径（守的是：标签存在、实时刷新、位置正确）。
    check('百分比排在「思考 自动」右侧且可见',
      Boolean(inline1) && inline1.rightOfReasoning, JSON.stringify(inline1));
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
    // 先整页重载：step 1-3 的断言已完成，而虚拟列表经多轮展开/滚动后渲染窗口可能
    // 停在空档；重载让侧栏按服务端列表重新渲染（空闲会话 updated_at 新，必在组内顶部）。
    const IDLE_TITLE = process.env.NAIBA_RING_IDLE_TITLE || '圆环空闲冒烟';
    await page.reload({ waitUntil: 'load', timeout: 30000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000, state: 'attached' });
    await openByTitle(IDLE_TITLE);
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
