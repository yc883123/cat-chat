// 输入框自增高几何守门（静态 public/ 服务，无需后端）——§九.128 的回归防线。
//
// 报障现场（2026-09-22 用户手机截图）：用了插话之后，**一个字都没有的输入框**占满下半屏。
// 根因：`resizeTextarea()` 用 `textarea.scrollHeight` 量高，而 Chrome 把**折行后的 placeholder**
// 也算成内容高度——手机 360px 下「回复进行中…（输入后 Enter 加入插话队列）」这句 23 字占位符
// 配上插话键挤到 88px 的输入区，空值被量成 155px。桌面 composer 宽约 880px，同一句一行放得下，
// 所以这个坑只在手机上出现（这就是「桌面看着好好的」的原因）。
//
// 为什么静态服务就够：本条只考**输入框几何 + resizeTextarea / updateContextComposerLock 的行为**，
// 与后端配置无关（§九.92 那条「静态服务让配置驱动 UI 消失」的教训不适用）。真后端下的整链路由
// `verify/interjection_smoke.py`（入队 / 引导 / 冻结）与手工探针覆盖。
//
// 用法：$env:NODE_PATH="<node_modules>"; node verify\composer_height_smoke.cjs
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.NAIBA_COMPOSER_SMOKE_PORT || 8794);
const BASE = `http://127.0.0.1:${PORT}`;
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const BUSY_PLACEHOLDER = '回复进行中…（输入后 Enter 加入插话队列）';
const ONE_LINE_MAX = 60;          // 一行高（16px 字号 → 23.2 行高 + 16 内边距 ≈ 39）+ 余量

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
        if (r.ok) resolve();
        else throw new Error('not ok');
      }).catch(() => {
        if (Date.now() - start > deadline * 1000) reject(new Error('server timeout'));
        else setTimeout(once, 300);
      });
    };
    once();
  });
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
    // 与用户的手机同形态：360×780 CSS · DPR3 · touch
    const context = await browser.newContext({
      viewport: { width: 360, height: 780 },
      deviceScaleFactor: 3,
      isMobile: true,
      hasTouch: true,
    });
    const page = await context.newPage();
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(`pageerror: ${e.message}`));
    page.on('console', (msg) => {
      if (msg.type() === 'error' && !msg.text().includes('404')) pageErrors.push(`console.error: ${msg.text()}`);
    });
    await page.addInitScript(() => { try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) {} });
    await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
    await page.waitForSelector('#messageInput', { timeout: 15000 });
    await page.waitForTimeout(600);

    // 页面内助手：拿到真模块，走真函数，不做任何 DOM 级"手写等价物"。
    const api = await page.evaluate(async () => {
      const core = await import('./js/01-core.js');
      // eslint-disable-next-line no-undef
      window.__m = {
        core,
        refs: await import('./js/13-skill-refs.js'),
        media: await import('./js/03-media.js'),
      };
      const t = document.querySelector('#messageInput');
      const c = document.querySelector('#composerForm');
      window.__probe = {
        oneLine() {                       // 「真实一行高」基准：摘掉占位符再读 scrollHeight
          const keep = t.placeholder;
          t.placeholder = '';
          t.style.height = 'auto';
          const h = t.scrollHeight;
          t.placeholder = keep;
          t.style.height = 'auto';
          return h;
        },
        snap() {
          return {
            inline: t.style.height,
            inlinePx: parseFloat(t.style.height) || 0,
            composerH: c.getBoundingClientRect().height,
            taW: t.getBoundingClientRect().width,
            scrollH: t.scrollHeight,          // 注意：占位符在场时这个值本身就被污染，正是本 bug 的证据
            value: t.value.length,
            placeholder: t.placeholder,
            interjectHidden: document.querySelector('#interjectButton').hidden,
          };
        },
        setValue(v) { t.value = v; },
        resize() { window.__m.refs.resizeTextarea(); },
        lock(busy) { window.__m.core.state.chatBusy = busy; window.__m.media.updateContextComposerLock(busy); },
      };
      return { ok: true };
    });
    check('① 页面模块装配（01-core / 13-skill-refs / 03-media 可 import）', api.ok === true);
    const snap = () => page.evaluate(() => window.__probe.snap());
    const oneLine = await page.evaluate(() => window.__probe.oneLine());
    const setValue = (v) => page.evaluate((x) => window.__probe.setValue(x), v);
    const resize = () => page.evaluate(() => window.__probe.resize());
    const lock = (busy) => page.evaluate((b) => window.__probe.lock(b), busy);

    // ── 前提对照：占位符真的会进入 scrollHeight（没有这条，下面的断言可能只是空测）
    await setValue('');
    await page.evaluate((ph) => { document.querySelector('#messageInput').placeholder = ph; }, BUSY_PLACEHOLDER);
    const polluted = (await snap()).scrollH;
    check('② 前提成立：运行中长占位符会把空框的 scrollHeight 撑到远超一行',
      polluted > oneLine + 40, `scrollHeight=${polluted} 一行=${oneLine}`);

    // ── 核心：同一个状态下量高，空框必须还是一行高（修复前这里会是 155）
    await resize();
    let s = await snap();
    check('③ 空输入框（长占位符在场）量高后仍是一行高',
      s.inlinePx > 0 && s.inlinePx <= ONE_LINE_MAX && s.composerH <= ONE_LINE_MAX + 20,
      JSON.stringify(s));

    // ── 有内容时按内容长高（且不被占位符污染）
    await lock(true);                          // 运行中：占位符换长句 + 插话键出现（输入区变窄）
    const busyLayout = await snap();
    await setValue('第一版方向可以，但封面先别截图，改用竖版重出一版看看');
    await resize();
    const typedBusy = await snap();
    check('④ 运行中打字：按内容长高，且超过一行',
      typedBusy.inlinePx > oneLine && typedBusy.inlinePx <= 180, JSON.stringify(typedBusy));
    check('⑤ 运行中插话键在场（输入区被挤窄）', busyLayout.interjectHidden === false
      && typedBusy.taW < 140, `插话键=${busyLayout.interjectHidden} 宽=${typedBusy.taW}`);

    // ── 宽度变化必须重算：本轮结束（插话键收起、输入区变宽）后同一段草稿要变矮
    await lock(false);
    const typedIdle = await snap();
    check('⑥ 本轮结束：占位符换回短句、插话键收起（接线真的走通了）',
      typedIdle.interjectHidden === true && typedIdle.placeholder === '输入消息',
      JSON.stringify(typedIdle));
    check('⑦ 同一段草稿在变宽的输入区里被重新量高（高度必须回落）',
      typedIdle.inlinePx < typedBusy.inlinePx,
      `窄=${typedBusy.inlinePx} 宽=${typedIdle.inlinePx}`);
    check('⑧ 草稿本身没被这两次重算吃掉', typedIdle.value === '第一版方向可以，但封面先别截图，改用竖版重出一版看看'.length);

    // ── 用户截图那一帧：值已清空 + 占位符已回短句 → 必须是一行高（不是残留的大高度）
    await setValue('');
    await lock(true);
    s = await snap();
    check('⑨ 运行中清空（入队路径的两行：value="" + resizeTextarea）→ 回到一行高',
      s.inlinePx <= ONE_LINE_MAX && s.composerH <= ONE_LINE_MAX + 20, JSON.stringify(s));
    await lock(false);
    s = await snap();
    check('⑩ 本轮结束（空框 + 短占位符）→ 仍是一行高，不残留',
      s.inlinePx <= ONE_LINE_MAX && s.composerH <= ONE_LINE_MAX + 20, JSON.stringify(s));

    // ── 上限与最短：极长文本封顶 180，清空回一行
    await setValue('长'.repeat(400));
    await resize();
    const huge = await snap();
    check('⑪ 极长文本封顶 180px（不把输入框顶穿屏幕）', huge.inlinePx === 180, JSON.stringify(huge));
    await setValue('');
    await resize();
    const cleared = await snap();
    check('⑫ 程序化清空后回到一行高', cleared.inlinePx <= ONE_LINE_MAX, JSON.stringify(cleared));

    check('⑬ 零 pageerror / 零 console.error（404 噪音除外）', pageErrors.length === 0, pageErrors.join(' | '));

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
