// A 级针对性检查：工具集展开后「紧凑度 + 父子区分度」。
// 断言：子卡片白底 + 强描边 + 投影、父展开区浅灰底、两者背景色差 ≥ 8、卡片高度明显变小。
// 运行：$env:NODE_PATH="<node_modules>"; node verify\_check_tool_cards_compact.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  // 8790 隔离配置里 providers 只有占位 base_url ⇒ 全新 profile 首启必弹 #onboardingDialog，
  // showModal 的 top layer 拦掉后续点击（"dialog intercepts pointer events"）。
  // 与 agent_prompt_smoke / tool_groups_smoke 同款：initScript 先置 dismissed 标记。
  await page.addInitScript(() => {
    try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (error) { void error; }
  });
  const errors = [];
  page.on('pageerror', (err) => errors.push(err.message));
  // 「Failed to load resource」不带 URL、没法与已知噪音区分：8790 的占位供应商让设置页的
  // 模型目录探测必然 POST /api/providers/models 400 —— 同款豁免；真实资源问题由逐条带
  // URL 的 404 监听兜住（本脚本未启用，资源缺失多表现为选择器超时）。
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().includes('Failed to load resource')) errors.push(msg.text());
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(800);
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForSelector('#agentCards .agent-card', { timeout: 10000 });
    await page.click('#agentCards [data-agent-card] .agent-card-name');
    await page.waitForSelector('#agentDialog[open]', { timeout: 10000 });
    await page.waitForTimeout(2000);
    // 分区切换后默认停在「基本」，工具卡片断言要先切到「工具集」页。
    await page.click('[data-agent-tab="tools"]');
    await page.waitForTimeout(300);
    // 卡片态默认不展开工具列表：点「添加自定义工具集」卡进入编辑态。
    await page.click('[data-tool-preset-add]');
    await page.waitForTimeout(500);

    // 展开前：分组头保持原样（透明/白底）
    const collapsedHead = await page.evaluate(() => {
      const head = document.querySelector('#agentToolScope .agent-tool-group-head');
      return getComputedStyle(head).backgroundColor;
    });
    check('折叠状态分组头保持原样（无灰底）',
      collapsedHead === 'rgba(0, 0, 0, 0)' || collapsedHead === 'rgb(255, 255, 255)',
      collapsedHead);

    // 展开第一个分组
    await page.evaluate(() => {
      const head = document.querySelector('#agentToolScope .agent-tool-group-head');
      head.click();
    });
    await page.waitForTimeout(500);

    const report = await page.evaluate(() => {
      const grid = document.querySelector('#agentToolScope .agent-tool-group .permission-grid');
      const cards = [...grid.querySelectorAll('label')];
      const cardStyle = cards.length ? getComputedStyle(cards[0]) : null;
      const gridStyle = getComputedStyle(grid);
      const group = grid.closest('.agent-tool-group');
      const headStyle = getComputedStyle(group.querySelector('.agent-tool-group-head'));
      const rgb = (value) => (value.match(/\d+/g) || []).slice(0, 3).map(Number);
      const distance = (a, b) => Math.max(...a.map((v, i) => Math.abs(v - b[i])));
      return {
        cardCount: cards.length,
        cardHeights: cards.map((c) => Math.round(c.getBoundingClientRect().height)),
        cardBg: cardStyle ? cardStyle.backgroundColor : '',
        cardBorder: cardStyle ? cardStyle.borderTopColor : '',
        cardBorderWidth: cardStyle ? cardStyle.borderTopWidth : '',
        cardShadow: cardStyle ? cardStyle.boxShadow : '',
        cardPadding: cardStyle ? cardStyle.padding : '',
        gridBg: gridStyle.backgroundColor,
        headBg: headStyle.backgroundColor,
        groupBg: getComputedStyle(group).backgroundColor,
        headDistance: distance(rgb(headStyle.backgroundColor), rgb(gridStyle.backgroundColor)),
        columns: gridStyle.gridTemplateColumns.split(' ').filter(Boolean).length,
      };
    });

    check('工具卡片更紧凑（高度 ≤ 80px，原 min-height 68 + 大内边距）',
      report.cardHeights.every((h) => h <= 80), JSON.stringify(report.cardHeights));
    check('卡片内边距收紧（8px 10px）', report.cardPadding === '8px 10px', report.cardPadding);
    check('子卡片白底 + 强描边 + 投影',
      report.cardBg === 'rgb(255, 255, 255)' && report.cardBorderWidth === '1px' && report.cardShadow !== 'none',
      JSON.stringify({ bg: report.cardBg, border: report.cardBorder, shadow: report.cardShadow }));
    check('展开区为白底', report.gridBg === 'rgb(255, 255, 255)', report.gridBg);
    check('展开的那一组分组头变浅灰条子', report.headBg === 'rgb(241, 242, 247)', report.headBg);
    check('条子与展开区背景色差 ≥ 8（肉眼可分辨）', report.headDistance >= 8, String(report.headDistance));
    check('仍是两列网格', report.columns === 2, String(report.columns));

    await page.evaluate(() => {
      document.querySelector('#agentToolScope .agent-tool-group .permission-grid').scrollIntoView({ block: 'center' });
    });
    await page.waitForTimeout(400);
    await page.screenshot({ path: 'verify/tool_cards_compact.png' });
    check('零 pageerror / console.error', errors.length === 0, errors.slice(0, 3).join(' | '));
  } catch (error) {
    check('检查脚本未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`工具卡片紧凑度检查：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exitCode = failures.length ? 1 : 0;
})();
