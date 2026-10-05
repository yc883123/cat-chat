// 输入框「展开/折叠」守门（静态 public/ 服务，无头 Edge，双视口）——S1–S5 修复的回归防线。
//
// 缺陷形态（2026-10-05 本人两连报障）：点双箭头「完全没反应」——高度永远由内容决定
// （`resizeTextarea` 只量内容），展开只把上限 180px 抬到 72dvh；「你好」这种短草稿
// scrollHeight=39px，上限抬多高都用不上。手机端更糟：`toggleComposerExpanded()` 恒 focus
// ⇒ 点展开必弹软键盘，观感反而是「点了弹输入法」。
//
// 本文件钉住甲篇五条契约（细节见 docs/plans/2026-10-05-输入框展开失效与原生工具调用吞正文修复计划.md）：
//   S1 展开态有真实下限（可视视口 40%、绝对 200px），且上限/下限参照 visualViewport；
//   S2 粗指针（手机/平板）展开不抢焦点，细指针（桌面）保留「点完直接能打字」；
//   S5 按钮 = 18px 迷你键 + 45° 斜双箭头，浮在输入框右上角 margin 排水沟里。
// S3 的 241px 空框残留**不在本文件**（依赖本人真机取数，另轮修复）。
//
// ⚠️ 本机测不到真软键盘（Playwright 不模拟 visualViewport 收缩）：键盘态那半条由
// tests/test_composer_height.py::ComposerExpandS1S5GuardTests 的源码守门钉死（监听绑定/参照系），
// 此处只测几何、状态与焦点策略。
//
// 用法：$env:NODE_PATH="<node_modules>"; node verify\composer_expand_smoke.cjs
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.NAIBA_COMPOSER_EXPAND_SMOKE_PORT || 8796);
const BASE = `http://127.0.0.1:${PORT}`;
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const ONE_LINE_MAX = 60;   // 一行高 ≈39px + 余量（与 composer_height_smoke.cjs 同口径）
const SETTLE_MS = 250;

const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

// 页面内助手：量真 DOM 的几何/状态，算独立的期望值（不 import 私有常量模块 —— 期望值
// 按契约公式复算，产品侧改错公式时这里会照出差异）。
const PROBE = () => {
  const $ = (s) => document.querySelector(s);
  const rect = (el) => { const b = el.getBoundingClientRect(); return { x: b.x, y: b.y, w: b.width, h: b.height }; };
  const overlap = (a, b) => !(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y);
  // 与 13-skill-refs.js 的 MIN_COMPOSER_H_EXPANDED_RATIO=0.40 / _PX=200 / MAX_…=0.72
  // 同值——常量若改，这三处表达式也要改（源码守门在 tests/test_composer_height.py）。
  const visible = () => {
    const vv = window.visualViewport;
    return vv && vv.height ? Math.min(vv.height, window.innerHeight) : window.innerHeight;
  };
  window.__probe = {
    closeDialogs() {
      // 静态服务下 /api/bootstrap 必 404 ⇒ 05-bootstrap 会弹 #authDialog 模态；
      // 真实点击会被它拦住、Playwright 一路重试到超时（§九.134 的探针环境坑）。
      document.querySelectorAll('dialog[open]').forEach((d) => d.close());
    },
    visibleHeight() { return visible(); },
    expectedMin() { return Math.max(200, Math.round(visible() * 0.40)); },
    expectedCap() { return Math.max(this.expectedMin(), Math.round(visible() * 0.72)); },
    snap() {
      const ta = $('#messageInput');
      const btn = $('#expandComposer');
      const send = $('#sendButton');
      const rb = rect(btn), rs = rect(send);
      const hit = document.elementFromPoint(rb.x + rb.w / 2, rb.y + rb.h / 2);
      return {
        h: parseFloat(ta.style.height) || 0,
        aria: btn.getAttribute('aria-pressed'),
        expanded: Boolean(document.querySelector('.composer-wrap:not(.edit-placeholder)')?.classList.contains('is-expanded')),
        focus: document.activeElement?.id || document.activeElement?.tagName || '',
        overlap: overlap(rb, rs),
        hitButton: Boolean(hit && btn.contains(hit)),
        btnW: Math.round(rb.w), btnH: Math.round(rb.h),
      };
    },
  };
};

async function openPage(browser, contextOptions) {
  const ctx = await browser.newContext(contextOptions);
  const page = await ctx.newPage();
  const errors = [];
  // 静态 public/ 下 `/api/bootstrap` 必 404，而它是以**未捕获异常**形式冒出来的
  // （`pageerror: HTTP 404`）——只放行这一种「整条消息就是 HTTP 404」的形态。
  page.on('pageerror', (e) => { if (!/^HTTP 404$/.test(e.message)) errors.push(`pageerror: ${e.message}`); });
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().includes('404') && !msg.text().includes('Failed to load resource')) {
      errors.push(`console.error: ${msg.text()}`);
    }
  });
  await page.addInitScript(() => { try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) {} });
  await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
  await page.waitForSelector('#messageInput', { timeout: 15000 });
  await page.waitForTimeout(400);
  await page.evaluate(async () => {
    // eslint-disable-next-line no-undef
    window.__m = { core: await import('./js/01-core.js') };
  });
  await page.evaluate(PROBE);
  await page.evaluate(() => window.__probe.closeDialogs());
  return { ctx, page, errors };
}

async function runViewport(browser, label, contextOptions, expectCoarse) {
  console.log(`\n===== ${label} =====`);
  const { ctx, page, errors } = await openPage(browser, contextOptions);
  try {
    const coarse = await page.evaluate(() => window.__m.core.isCoarsePointer());
    check(`${label} ⓪ 前提：真模块 isCoarsePointer()=${expectCoarse}`, coarse === expectCoarse, String(coarse));

    // 报障现场：两字草稿「你好」。折叠态先确认基线是一行高（39px）。
    await page.fill('#messageInput', '你好');
    const colBase = await page.evaluate(() => window.__probe.snap());
    check(`${label} ⑨a 折叠态：展开键与发送键矩形不相交`, colBase.overlap === false);
    check(`${label} ⓑ 基线：折叠 + 「你好」= 一行高`, colBase.expanded === false && colBase.h <= ONE_LINE_MAX,
      `h=${colBase.h}`);

    // ①②③：点展开 —— 短草稿也必须**真变大**（本次修复的核心回归位；修复前 h 恒定 39）。
    await page.evaluate(() => window.__probe.closeDialogs());
    await page.click('#expandComposer');
    await sleep(SETTLE_MS);
    const ex = await page.evaluate(() => window.__probe.snap());
    const threshold = await page.evaluate(() => Math.round(window.__probe.visibleHeight() * 0.30));
    check(`${label} ① 点击后 aria-pressed="true"`, ex.aria === 'true', `aria=${ex.aria}`);
    check(`${label} ② 点击后 is-expanded 挂上`, ex.expanded === true);
    check(`${label} ③ 短草稿展开后高度 ≥ 可视高×30%（≥${threshold}px）`, ex.h >= threshold, `h=${ex.h}`);
    check(`${label} ⑨b 展开态：展开键与发送键矩形不相交`, ex.overlap === false);

    // ⑦：焦点策略（S2）——粗指针不抢焦点（防弹软键盘），细指针保持点完能打字。
    if (expectCoarse) {
      check(`${label} ⑦ 粗指针：展开不抢焦点（不再乱弹软键盘）`, ex.focus !== 'messageInput', `activeElement=${ex.focus}`);
    } else {
      check(`${label} ⑦ 细指针：展开后焦点直接落在输入框`, ex.focus === 'messageInput', `activeElement=${ex.focus}`);
    }

    // ④：灌 20 行 —— 内容驱动仍生效（高度只增不减），且不超展开上限。
    const lines20 = Array.from({ length: 20 }, (_, i) => `第${i + 1}行 草稿内容用来撑高度`);
    await page.fill('#messageInput', lines20.join('\n'));
    const h20 = await page.evaluate(() => window.__probe.snap());
    const cap = await page.evaluate(() => window.__probe.expectedCap());
    check(`${label} ④ 灌 20 行后高度 ≥ 展开初值（不回退）`, h20.h >= ex.h, `20行=${h20.h} 初值=${ex.h}`);
    check(`${label} ④b 高度不超展开上限（${cap}px，含舍入余量）`, h20.h <= cap + 1, `20行=${h20.h}`);

    // ⑧：逼出真滚动再滚到底（20 行=480px 达不到上限、根本不滚——桌面实测），
    // 对按钮中心做 elementFromPoint 必须仍命中按钮自身（margin 排水沟的回归防线：
    // 滚动条/文本永远够不到按钮；修复前若用 padding 让位，这条会红）。
    await page.evaluate(() => {
      const ta = document.querySelector('#messageInput');
      ta.value = Array.from({ length: 60 }, (_, i) => `第${i + 1}行 长内容用于逼出滚动`).join('\n');
      window.__m.core.notifyComposerChanged(ta);
    });
    await sleep(SETTLE_MS);
    await page.evaluate(() => {
      const ta = document.querySelector('#messageInput');
      ta.scrollTop = ta.scrollHeight;
    });
    await page.evaluate(() => window.__probe.closeDialogs());
    await sleep(120);
    const s8 = await page.evaluate(() => window.__probe.snap());
    check(`${label} ⑧ 长内容滚到底后，按钮中心 elementFromPoint 仍命中按钮自身`, s8.hitButton === true);

    // ⑤：展开态清空 —— 回落到 expandedMin（真实下限），不是 39px。
    await page.fill('#messageInput', '');
    await sleep(120);
    const emptyEx = await page.evaluate(() => window.__probe.snap());
    const emin = await page.evaluate(() => window.__probe.expectedMin());
    check(`${label} ⑤ 展开态清空后回落到 expandedMin=${emin}px（不是 39px）`,
      Math.abs(emptyEx.h - emin) <= 2 && emptyEx.h > ONE_LINE_MAX, `h=${emptyEx.h}`);

    // ⑤b / ⑥：收起 —— aria 回落 + 高度回一行（空框）。
    await page.click('#expandComposer');
    await sleep(SETTLE_MS);
    const collapsed = await page.evaluate(() => window.__probe.snap());
    check(`${label} ⑥ 收起后 aria-pressed="false"`, collapsed.aria === 'false', `aria=${collapsed.aria}`);
    check(`${label} ⑤b 收起后高度回落到 ≈39px（空框不残留大高度）`,
      collapsed.expanded === false && collapsed.h <= ONE_LINE_MAX, `h=${collapsed.h}`);

    check(`${label} ⑩ 零 pageerror / console.error（静态服务的 404 噪音除外）`, errors.length === 0, errors.join(' | '));
  } finally {
    await ctx.close();
  }
}

(async () => {
  const server = spawn(PY, ['-m', 'http.server', String(PORT), '--directory', path.join(ROOT, 'public')], {
    cwd: ROOT, windowsHide: true,
  });
  server.stderr.on('data', (d) => console.error(String(d).trim()));
  let code = 1;
  try {
    await waitForServer();
    const browser = await chromium.launch({ channel: 'msedge', headless: true });
    // 双视口：桌面 1584×1067（细指针）与手机 360×780 DPR3（粗指针）。
    await runViewport(browser, '桌面 1584×1067', { viewport: { width: 1584, height: 1067 } }, false);
    await runViewport(browser, '手机 360×780', {
      viewport: { width: 360, height: 780 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true,
    }, true);
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
