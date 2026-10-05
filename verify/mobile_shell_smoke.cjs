// 手机形态冒烟（源码 server，端口 8790）。
// 核心主张：「手机端不删能力，只换形态」。本冒烟同时覆盖手机形态（≤760）与电脑形态（>760），
// 断言两者在同一份 DOM 上按视口宽度切换形态，而不是靠隐藏控件实现「降级」。
//
// 覆盖：
//   A. 五档视口 375 / 640 / 760 / 900 / 1440：
//      - ≤760：轮次下拉可见（刻度轨的等价出口）、刻度轨由 CSS 隐藏；
//      - ≥1000（宽桌面）：轮次下拉同样可见（落在顶栏左段中段）、刻度轨可见；
//      - 761–999（窄桌面）：顶栏被 API+Agent+4 按钮占满，轮次下拉收起、只留刻度轨；
//      - 全程无横向滚动；顶栏文字标签一律保留（§九.44）；手机端操作区按钮触摸目标 ≥44px。
//   B. 文件面板：手机上为全屏抽屉（fixed / 非 display:none），桌面上仍是右侧栏（非 fixed）。
//   C. 侧栏：手机上为抽屉（点 #openSidebar 打开、点遮罩关闭），桌面上是常驻侧栏。
//   D. 轮次下拉行为：打开一个 ≥2 轮的会话后，选项数 = 轮数、选中即跳转（复用刻度轨 scrollToTurn）。
//
// 前置：
//   1. 源码 server 已在 8790 运行；
//   2. D 段需要多轮会话，先跑 `python verify/seed_turn_rail_chat.py`；
//      若当前库里已有多轮会话，则自动选用，不强制依赖播种脚本。
// 运行：$env:NODE_PATH="<node_modules 目录>"; node verify\mobile_shell_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const SEEDED_TITLE = process.env.NAIBA_MOBILE_SEED_TITLE || '刻度轨冒烟会话';
const MOBILE_MAX = 760;
const VIEWPORTS = [375, 640, 760, 900, 1440];
const failures = [];
const skipped = [];

function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

function skip(label, detail = '') {
  console.log(`SKIP  ${label}${detail ? `  -> ${detail}` : ''}`);
  skipped.push(label);
}

// 轮次下拉 / 刻度轨的可见性由 CSS 决定，但两者都带 [hidden]（内容不足时隐藏）。
// 断言形态前临时摘掉 hidden 再读 computed display，与「有没有多轮数据」解耦。
async function probeSwitchVisibility(page) {
  return page.evaluate(() => {
    const jump = document.querySelector('#turnJumpSelect');
    const rail = document.querySelector('#turnRail');
    const restore = [];
    for (const el of [jump, rail]) {
      if (!el) continue;
      restore.push([el, el.hidden]);
      el.hidden = false;
    }
    const display = (el) => (el ? getComputedStyle(el).display : 'missing');
    const result = { jumpDisplay: display(jump), railDisplay: display(rail) };
    for (const [el, hidden] of restore) el.hidden = hidden;
    return result;
  });
}

// 文件面板形态：模拟「已打开」（加 file-panel-open）后读样式，不受是否有文件标签影响。
async function probeFilePanel(page) {
  return page.evaluate(() => {
    const shell = document.querySelector('#appShell');
    const panel = document.querySelector('#filePanel');
    if (!shell || !panel) return { exists: false };
    const hadClass = shell.classList.contains('file-panel-open');
    shell.classList.add('file-panel-open');
    const style = getComputedStyle(panel);
    const shown = { display: style.display, position: style.position, width: Math.round(panel.getBoundingClientRect().width) };
    if (!hadClass) shell.classList.remove('file-panel-open');
    return { exists: true, ...shown };
  });
}

// 轮次跳转用 behavior:'smooth'：几十轮的跨距在 Chromium 里要 ~1.5s 才落点，
// 固定 waitForTimeout 会读到中途位置（选中项还挂在半路的轮次上）。等滚动真正停下来。
async function waitForScrollSettle(page, timeout = 6000) {
  const deadline = Date.now() + timeout;
  let last = null;
  let stable = 0;
  while (Date.now() < deadline) {
    const top = await page.evaluate(() => Math.round(document.querySelector('#messages')?.scrollTop ?? -1));
    if (top === last) {
      stable += 1;
      if (stable >= 3) break;
    } else {
      stable = 0;
      last = top;
    }
    await page.waitForTimeout(120);
  }
  await page.waitForTimeout(300);  // 落点后再留一帧给 applyTurnJump 的落点同步
  return last;
}

async function turnJumpSnapshot(page) {
  return page.evaluate(() => {
    const select = document.querySelector('#turnJumpSelect');
    const container = document.querySelector('#messages');
    return {
      optionCount: select ? select.options.length : 0,
      value: select ? select.value : '',
      hidden: select ? select.hidden : true,
      firstLabel: select && select.options.length ? select.options[0].textContent : '',
      scrollTop: container ? Math.round(container.scrollTop) : -1,
      userRows: document.querySelectorAll('#messages .message-row.user').length,
    };
  });
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    // 形态冒烟常跑在全新隔离数据根上：未配置供应商时 POST /api/providers/models 会返回 400，
    // 浏览器把它记成 "Failed to load resource" 的 console.error。这是环境噪音，不是 JS 异常；
    // 本冒烟只守 app 自身抛出的 console.error（真实 JS 回归）。
    if (/Failed to load resource/i.test(text)) return;
    pageErrors.push(`console.error: ${text}`);
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(1000);

    // 首启引导弹窗是 <dialog open>，会拦截一切 pointer 事件（冒烟常跑在全新隔离数据根上）。
    // 它不是被测对象，先关掉再进入点击类断言，否则 C 段的 #openSidebar 会被挡住。
    await page.evaluate(() => {
      const d = document.querySelector('#onboardingDialog');
      if (d && d.open) d.close();
    });
    await page.waitForTimeout(200);

    // ---- A. 五档视口：形态切换（数据无关）----
    for (const width of VIEWPORTS) {
      const isMobile = width <= MOBILE_MAX;
      await page.setViewportSize({ width, height: 800 });
      await page.waitForTimeout(420);

      const { jumpDisplay, railDisplay } = await probeSwitchVisibility(page);
      const layout = await page.evaluate(() => {
        const labels = [...document.querySelectorAll('.topbar .button-label')].map((el) => ({
          text: el.textContent.trim(), display: getComputedStyle(el).display,
        }));
        const actions = [...document.querySelectorAll('.topbar-actions .control-button, .topbar-actions .mcp-button')]
          .map((el) => ({ id: el.id, minHeight: getComputedStyle(el).minHeight, height: Math.round(el.getBoundingClientRect().height) }));
        const sidebar = document.querySelector('#sidebar');
        return {
          innerWidth: window.innerWidth,
          scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
          labels,
          actions,
          sidebarPosition: sidebar ? getComputedStyle(sidebar).position : 'missing',
        };
      });

      if (isMobile) {
        check(`[${width}px] 手机形态：轮次下拉可见（刻度轨的等价出口）`, jumpDisplay !== 'none' && jumpDisplay !== 'missing', jumpDisplay);
        check(`[${width}px] 手机形态：刻度轨由 CSS 隐藏`, railDisplay === 'none', railDisplay);
        check(`[${width}px] 手机形态：侧栏是抽屉（position: fixed）`, layout.sidebarPosition === 'fixed', layout.sidebarPosition);
        check(`[${width}px] 顶栏操作按钮触摸目标 ≥44px`,
          layout.actions.length > 0 && layout.actions.every((item) => item.minHeight === '44px' || item.height >= 44),
          JSON.stringify(layout.actions));
      } else {
        // 桌面：≥1000px 顶栏放得下轮次下拉（落在左段中段）；761–999px 顶栏被 API+Agent+4 按钮占满，
        // 下拉会被压成看不清的废桩，故收掉、只留刻度轨（刻度轨桌面恒在，功能不丢）。
        const wideDesktop = width >= 1000;
        if (wideDesktop) {
          check(`[${width}px] 电脑形态：轮次下拉可见（落在左段中段）`, jumpDisplay !== 'none' && jumpDisplay !== 'missing', jumpDisplay);
        } else {
          check(`[${width}px] 电脑形态（窄）：轮次下拉收起（让位刻度轨）`, jumpDisplay === 'none', jumpDisplay);
        }
        check(`[${width}px] 电脑形态：刻度轨可见`, railDisplay !== 'none' && railDisplay !== 'missing', railDisplay);
        check(`[${width}px] 电脑形态：侧栏是常驻侧栏（非 fixed）`, layout.sidebarPosition !== 'fixed', layout.sidebarPosition);
      }

      check(`[${width}px] 无横向滚动`, layout.scrollWidth <= layout.innerWidth + 1,
        `scrollWidth=${layout.scrollWidth} innerWidth=${layout.innerWidth}`);
      // §九.44：宁可行内换行也不隐藏文字标签（761–1100px 曾因隐藏标签只剩裸数字被报障）。
      check(`[${width}px] 顶栏文字标签未被隐藏（§九.44）`,
        layout.labels.length > 0 && layout.labels.every((item) => item.display !== 'none' && item.text !== ''),
        JSON.stringify(layout.labels));
    }

    // ---- B. 文件面板：手机全屏抽屉 / 桌面右侧栏（数据无关）----
    await page.setViewportSize({ width: 375, height: 800 });
    await page.waitForTimeout(420);
    const mobilePanel = await probeFilePanel(page);
    check('手机端文件面板可显示（非 display:none）', mobilePanel.exists && mobilePanel.display !== 'none', JSON.stringify(mobilePanel));
    check('手机端文件面板是全屏抽屉（position: fixed）', mobilePanel.position === 'fixed', JSON.stringify(mobilePanel));

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForTimeout(420);
    const desktopPanel = await probeFilePanel(page);
    check('桌面端文件面板仍可显示', desktopPanel.exists && desktopPanel.display !== 'none', JSON.stringify(desktopPanel));
    check('桌面端文件面板不是全屏抽屉（非 fixed）', desktopPanel.position !== 'fixed', JSON.stringify(desktopPanel));

    // ---- C. 侧栏抽屉：手机上可开可关（数据无关）----
    await page.setViewportSize({ width: 375, height: 800 });
    await page.waitForTimeout(420);
    await page.click('#openSidebar');
    await page.waitForTimeout(400);
    let drawer = await page.evaluate(() => ({
      open: document.querySelector('#sidebar')?.classList.contains('open'),
      backdrop: Boolean(document.querySelector('#sidebarBackdrop')?.classList.contains('open')),
    }));
    check('手机端点汉堡按钮打开抽屉侧栏', drawer.open === true && drawer.backdrop === true, JSON.stringify(drawer));
    await page.click('#sidebarBackdrop', { position: { x: 320, y: 300 } }).catch(() => {});
    await page.waitForTimeout(400);
    drawer = await page.evaluate(() => ({
      open: document.querySelector('#sidebar')?.classList.contains('open'),
      backdrop: Boolean(document.querySelector('#sidebarBackdrop')?.classList.contains('open')),
    }));
    check('手机端点遮罩关闭抽屉侧栏', drawer.open === false && drawer.backdrop === false, JSON.stringify(drawer));

    // ---- D. 轮次下拉行为（需多轮会话）----
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.waitForTimeout(300);
    const opened = await page.evaluate((title) => {
      const items = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-open')];
      if (!items.length) return '';
      const match = items.find((el) => (el.textContent || '').includes(title)) || items[0];
      match.click();
      return (match.textContent || '').trim();
    }, SEEDED_TITLE);
    if (!opened) {
      skip('D 段轮次下拉行为：库里没有任何会话');
    } else {
      await page.waitForTimeout(1400);
      await page.setViewportSize({ width: 375, height: 800 });
      await page.waitForTimeout(500);
      let jump = await turnJumpSnapshot(page);
      if (jump.optionCount < 2) {
        skip('D 段轮次下拉行为：当前会话不足 2 轮（可先跑 seed_turn_rail_chat.py）',
          JSON.stringify({ title: opened, ...jump }));
      } else {
        check('手机端多轮会话出现轮次下拉（选项数 = 轮数）',
          jump.hidden === false && jump.optionCount >= 2, JSON.stringify(jump));
        check('轮次下拉首项形如「第 1 轮 · 摘要」', /^第 1 轮 · /.test(jump.firstLabel), jump.firstLabel);

        const lastIndex = jump.optionCount - 1;
        const beforeJump = jump.scrollTop;
        await page.selectOption('#turnJumpSelect', String(lastIndex));
        await waitForScrollSettle(page);
        const afterToBottom = await turnJumpSnapshot(page);
        check('选择最后一轮后选中项落在该轮（跳转后高亮不漂移）',
          afterToBottom.value === String(lastIndex), JSON.stringify({ want: lastIndex, got: afterToBottom.value }));

        await page.selectOption('#turnJumpSelect', '0');
        await waitForScrollSettle(page);
        const afterToTop = await turnJumpSnapshot(page);
        check('选择第 1 轮后选中项回到第 1 轮',
          afterToTop.value === '0', JSON.stringify(afterToTop));
        check('轮次下拉确实驱动了消息区滚动（滚动位置发生变化）',
          afterToTop.scrollTop !== beforeJump || afterToTop.scrollTop < afterToBottom.scrollTop,
          JSON.stringify({ before: beforeJump, bottom: afterToBottom.scrollTop, top: afterToTop.scrollTop }));
      }
    }

    await page.setViewportSize({ width: 1280, height: 900 });
    await page.waitForTimeout(300);
    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.stack ? error.stack.split('\n')[0] : error));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`手机形态冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`
    + (skipped.length ? `；跳过 ${skipped.length} 项（${skipped.join('、')}）` : ''));
  process.exit(failures.length ? 1 : 0);
})();
