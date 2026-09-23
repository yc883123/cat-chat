// 手机运行态输入区版式守门（静态 public/ 服务，无需后端）——§九.135 第 6 项的回归防线。
//
// 背景：第 1 项把手机裸回车改成换行之后，`#interjectButton` 从「备选入口」升格为运行中
// **唯一**的插话入口（运行中 `#sendButton` 已变身「停止」）。而它当时是个 38×38、没有文字、
// 图标还画不出来的小按钮：
//   · 图标从来没画出来过——`styles.css` 只给 `.composer .interject-button svg` 设了尺寸，
//     而 `fill:none; stroke:currentColor` 那条挂在 `.icon-button svg`；插话键没有 icon-button 类，
//     于是回落到 SVG 默认 `fill: rgb(0,0,0)` / `stroke: none` ⇒ 主干与底线不可见、箭头被填成实心 ▾；
//   · 38×38 低于手机 44px 最小点按尺寸（同一条漏网的还有上下文圆环）。
// 修法：运行中（`.composer.is-running`，只在 ≤760px 生效）把输入区拆成独立一行、插话键
// 拿到「插话」二字并长到 44px，图标补 paint 属性并整枚垂直翻转（队列就在输入框上方）。
//
// 为什么静态服务就够：本条考的全是**几何与计算样式**，与后端配置无关（§九.92 那条教训不适用）。
// 真后端下的插话整链路由 `verify/interjection_smoke.py` 覆盖。
//
// 用法：$env:NODE_PATH="<node_modules>"; node verify\composer_running_layout.cjs
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.NAIBA_COMPOSER_LAYOUT_PORT || 8816);
const BASE = `http://127.0.0.1:${PORT}`;
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const MIN_TAP = 44;               // 手机最小点按尺寸
const MIN_LABEL_W = 69;           // 带「插话」二字的按钮最小宽（探针实测 71，留 2px 余量）
const FULL_ROW_MIN = 300;         // 「输入区独占一行」的宽度下限（360 - 左右内边距）

// 量之前必须等版式稳定：`.composer .icon-button` / `.send-button` 都带 `transition: var(--t-fast)`
// （= 120ms all），而 `order` 是可动画的整型属性 ⇒ 拆行/回单行是一次**真过渡**。
// 立刻量会拿到过渡起点（计算 order 还是 0、版式是中间态的第 3 行），得到一条与产品无关的假红。
const SETTLE_MS = 300;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
};

function waitForServer(deadline = 15) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const once = () => {
      fetch(`${BASE}/index.html`).then((r) => {
        if (r.ok) resolve(); else throw new Error('not ok');
      }).catch(() => {
        if (Date.now() - start > deadline * 1000) reject(new Error('server timeout'));
        else setTimeout(once, 300);
      });
    };
    once();
  });
}

// 页面内助手：拿真模块、调真函数，不做任何 DOM 级「手写等价物」。
const PROBE = () => {
  const t = document.querySelector('#messageInput');
  const c = document.querySelector('#composerForm');
  const attach = document.querySelector('#attachButton');
  const interject = document.querySelector('#interjectButton');
  const label = interject.querySelector('.interject-label');
  const svg = interject.querySelector('svg');
  const arrow = svg.querySelectorAll('path')[1];
  const rect = (el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; };
  window.__probe = {
    snap() {
      const ir = rect(t);
      const ar = rect(attach);
      const br = rect(interject);
      const cs = getComputedStyle(svg);
      const csArrow = getComputedStyle(arrow);
      // 沿箭头路径采样，取最高点（y 最小）：朝上的箭头，尖点必须比两个端点都高。
      const len = arrow.getTotalLength();
      let minY = Infinity;
      let endA = 0; let endB = 0;
      for (let i = 0; i <= 40; i += 1) {
        const p = arrow.getPointAtLength((len * i) / 40);
        if (i === 0) endA = p.y;
        if (i === 40) endB = p.y;
        if (p.y < minY) minY = p.y;
      }
      return {
        isRunning: c.classList.contains('is-running'),
        input: ir,
        attach: ar,
        interject: br,
        // 同一行 ⟺ 两个矩形在纵向上有重叠
        sameRow: ar.y < ir.y + ir.h && ir.y < ar.y + ar.h,
        // 输入区独占一行 ⟺ 它的下边缘不越过附件键的上边缘
        inputAboveRow: ir.y + ir.h <= ar.y + 0.5,
        interjectHidden: interject.hidden,
        labelDisplay: getComputedStyle(label).display,
        labelVisible: label.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }),
        labelText: label.textContent.trim(),
        svgFill: cs.fill,
        svgStroke: cs.stroke,
        arrowFill: csArrow.fill,
        arrowTipAboveEnds: minY < endA - 2 && minY < endB - 2,
        arrowEnds: [endA, endB],
        arrowMinY: minY,
        inlineH: parseFloat(t.style.height) || 0,
      };
    },
    setBusy(busy) {
      window.__m.core.state.chatBusy = busy;
      window.__m.media.updateContextComposerLock(busy);
    },
  };
};

(async () => {
  const server = spawn(PY, ['-m', 'http.server', String(PORT), '--directory', path.join(ROOT, 'public')], {
    cwd: ROOT, windowsHide: true,
  });
  server.stderr.on('data', (d) => console.error(String(d).trim()));
  let code = 1;
  try {
    await waitForServer();
    const browser = await chromium.launch({ channel: 'msedge', headless: true });
    const pageErrors = [];

    // ── A. 手机形态：360×780 · DPR3 · touch（与用户手机同形）
    const mobile = await browser.newContext({
      viewport: { width: 360, height: 780 },
      deviceScaleFactor: 3,
      isMobile: true,
      hasTouch: true,
    });
    const page = await mobile.newPage();
    page.on('pageerror', (e) => pageErrors.push(`pageerror: ${e.message}`));
    page.on('console', (msg) => {
      if (msg.type() === 'error' && !msg.text().includes('404')) pageErrors.push(`console.error: ${msg.text()}`);
    });
    await page.addInitScript(() => { try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) {} });
    await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
    await page.waitForSelector('#messageInput', { timeout: 15000 });
    await page.waitForTimeout(500);

    await page.evaluate(async () => {
      // eslint-disable-next-line no-undef
      window.__m = { core: await import('./js/01-core.js'), media: await import('./js/03-media.js') };
    });
    await page.evaluate(PROBE);
    const snap = () => page.evaluate(() => window.__probe.snap());
    const setBusy = async (b) => { await page.evaluate((x) => window.__probe.setBusy(x), b); await sleep(SETTLE_MS); };

    // ── 前提：空闲态必须是**单行**，而且 is-running 不在场。缺了这一条，后面「拆成两行」
    //    的断言可能只是因为页面本来就是两行而空转。
    await setBusy(false);
    let s = await snap();
    check('① 前提成立：空闲态 .composer 是单行（附件键与输入区同一 y）且无 is-running',
      s.isRunning === false && s.sameRow === true && s.inputAboveRow === false,
      JSON.stringify({ isRunning: s.isRunning, sameRow: s.sameRow, input: s.input, attach: s.attach }));

    // ── 运行中：拆两行 + 插话键可辨识
    await setBusy(true);
    s = await snap();
    check('② 运行中：#composerForm 带 is-running（类由 updateContextComposerLock 单点翻转）',
      s.isRunning === true);
    check(`③ 输入区独占一整行（下边缘不越过附件键上边缘）且宽度 ≥ ${FULL_ROW_MIN}px`,
      s.inputAboveRow === true && s.input.w >= FULL_ROW_MIN,
      JSON.stringify({ inputAboveRow: s.inputAboveRow, input: s.input, attach: s.attach, interject: s.interject }));
    check(`④ 插话键 ≥${MIN_TAP}×${MIN_TAP}（手机最小点按尺寸）`,
      s.interject.w >= MIN_TAP && s.interject.h >= MIN_TAP,
      JSON.stringify(s.interject));
    check(`⑤ 插话键带文字「插话」且够宽（≥${MIN_LABEL_W}px）`,
      s.labelVisible === true && s.labelText === '插话' && s.interject.w >= MIN_LABEL_W,
      JSON.stringify({ visible: s.labelVisible, text: s.labelText, w: s.interject.w }));

    // ── 图标真的画了出来（旧代码这里是 fill: rgb(0,0,0) / stroke: none）
    check('⑥ 图标 paint 正确：stroke ≠ none 且 fill = none（旧代码正好相反）',
      s.svgStroke !== 'none' && s.svgFill === 'none' && s.arrowFill === 'none',
      JSON.stringify({ stroke: s.svgStroke, fill: s.svgFill, arrowFill: s.arrowFill }));
    check('⑦ 箭头朝上（尖点比两个端点都高）：队列就在输入框上方',
      s.arrowTipAboveEnds === true,
      JSON.stringify({ ends: s.arrowEnds, minY: s.arrowMinY }));

    // ── 空闲回归：标签收起、版式回到单行量级（is-running 一旦泄到空闲态就是全局版式事故）
    await setBusy(false);
    s = await snap();
    check('⑧ 空闲回归：is-running 摘掉、输入区回到与附件键同一行',
      s.isRunning === false && s.sameRow === true,
      JSON.stringify({ isRunning: s.isRunning, sameRow: s.sameRow }));
    check('⑨ 空闲回归：「插话」二字不可见（只许在手机运行态出现）',
      s.labelVisible === false && s.labelDisplay === 'none',
      JSON.stringify({ visible: s.labelVisible, display: s.labelDisplay }));

    // ── B. 桌面回归（1440）：is-running 会在场，但版式与标签一个都不许变
    const desktop = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const dpage = await desktop.newPage();
    dpage.on('pageerror', (e) => pageErrors.push(`desktop pageerror: ${e.message}`));
    await dpage.addInitScript(() => { try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) {} });
    await dpage.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
    await dpage.waitForSelector('#messageInput', { timeout: 15000 });
    await dpage.waitForTimeout(400);
    await dpage.evaluate(async () => {
      // eslint-disable-next-line no-undef
      window.__m = { core: await import('./js/01-core.js'), media: await import('./js/03-media.js') };
    });
    await dpage.evaluate(PROBE);
    await dpage.evaluate(() => window.__probe.setBusy(true));
    await sleep(SETTLE_MS);
    const ds = await dpage.evaluate(() => window.__probe.snap());
    check('⑩ 桌面(1440)运行中：版式仍是单行、插话键仍是方形小按钮、无文字标签',
      ds.sameRow === true && ds.inputAboveRow === false
      && ds.labelVisible === false && ds.interject.w < 44 && ds.interject.h < 44,
      JSON.stringify({ sameRow: ds.sameRow, w: ds.interject.w, h: ds.interject.h, label: ds.labelVisible }));
    check('⑪ 桌面图标同样修好了（paint + 箭头朝上，全平台同一个字形）',
      ds.svgStroke !== 'none' && ds.svgFill === 'none' && ds.arrowTipAboveEnds === true,
      JSON.stringify({ stroke: ds.svgStroke, fill: ds.svgFill, tip: ds.arrowTipAboveEnds }));

    check('⑫ 零 pageerror / 零 console.error（404 噪音除外）', pageErrors.length === 0, pageErrors.join(' | '));

    await browser.close();
    code = failures.length ? 1 : 0;
    console.log(failures.length ? `\n${failures.length} 条断言失败：${failures.join(' / ')}` : '\n全部通过');
  } catch (error) {
    console.error('异常：', error.message);
    code = 1;
  } finally {
    server.kill();
    setTimeout(() => process.exit(code), 200);
  }
})();
