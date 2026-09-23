// 「消息里文件 chip 的动作条」前端守门（静态 public/ 服务，无需后端）——§九.135 第 5 项。
//
// 缺陷形态：消息里的文件 chip 全局只有两条路——非媒体 chip 是 `<a target="_blank">` 预览，
// 「本轮修改文件」chip 是点开右侧文件面板。桌面端**没有任何**「用系统程序打开 / 在资源管理器里
// 定位 / 另存到别处」的入口；手机端连预览都没有（前者要长按、后者只是面板）。
//
// 本冒烟钉的是**前端这一半**，分两种形态各跑一遍（三件事都必须成立）：
//   桌面（有 pywebview 桥 / 1280px）：chip 悬停露出三个动作、图标真的可见、点击真的过桥、
//                                    chip 本体行为不变（点动作条不会顺带打开文件面板）；
//   手机（无桥 / 360px）：「⋯」常驻且 ≥44px、三个动作收进菜单、**没有**「打开文件夹」
//                        （非桌面环境在 markup 层就不渲染它）、另存为复用第 3 项的 download=1。
//
// 桥（launcher.py 的 JsApi）那一半由 tests/test_file_action_bridge.py 覆盖；这里只把
// `window.pywebview.api` 换成记账桩，验证"前端点了会不会叫、叫的是哪个方法、参数对不对"。
//
// 用法：$env:NODE_PATH="<node_modules>"; node verify\file_actions_smoke.cjs
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.NAIBA_FILE_ACTIONS_SMOKE_PORT || 8819);
const BASE = `http://127.0.0.1:${PORT}`;
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const TEXT_PATH = 'D:\\ws\\extract_conversation.py';
const CHIP_NAME = 'extract_conversation.py';

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

// 页面公共准备：拦下载、拦 window.open、收 pageerror，并把用到的模块挂到 window.__m。
async function preparePage(browser, { desktop }) {
  const context = await browser.newContext(desktop
    ? { viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 }
    : { viewport: { width: 360, height: 780 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', (e) => { if (!/^HTTP 404$/.test(e.message)) errors.push(`pageerror: ${e.message}`); });
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().includes('404') && !msg.text().includes('Failed to load resource')) {
      errors.push(`console.error: ${msg.text()}`);
    }
  });
  await page.addInitScript(() => { try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) {} });
  await page.addInitScript(() => {
    window.__downloads = [];
    const originalClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function patchedClick() {
      if (this.hasAttribute('download') || /[?&]download=1(?:&|$)/.test(this.href)) {
        window.__downloads.push({ href: this.href, name: this.getAttribute('download') || '' });
        return undefined;
      }
      return originalClick.call(this);
    };
    window.__opened = [];
    window.open = function patchedOpen(url) { window.__opened.push(String(url)); return null; };
  });
  if (desktop) {
    // 桌面形态：把桥换成记账桩（真桥要起 pywebview 窗口，跑不进冒烟）。
    await page.addInitScript(() => {
      window.__bridgeCalls = [];
      const record = (method) => async (...args) => { window.__bridgeCalls.push([method, ...args]); return { ok: true }; };
      window.pywebview = {
        api: {
          naibaOpenFile: record('naibaOpenFile'),
          naibaRevealInFolder: record('naibaRevealInFolder'),
          naibaSaveFileAs: record('naibaSaveFileAs'),
        },
      };
    });
  }
  await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
  await page.waitForSelector('#messageInput', { timeout: 15000 });
  await page.waitForTimeout(300);
  await page.evaluate(async () => {
    window.__m = {
      core: await import('./js/01-core.js'),
      media: await import('./js/03-media.js'),
      panel: await import('./js/14-file-panel.js'),
    };
    document.querySelectorAll('dialog[open]').forEach((d) => d.close());
  });
  // 渲染两条消息体：一条非媒体附件 chip（助手侧 mediaMarkup），一条「本轮修改文件」chip。
  await page.evaluate(({ textPath }) => {
    const media = window.__m.media;
    const messages = document.querySelector('#messages');
    messages.innerHTML = ''
      + `<div class="message-row" data-smoke="file-chip">${media.mediaMarkup([{ path: textPath, name: 'extract_conversation.py', source: textPath }])}</div>`
      + `<div class="message-row" data-smoke="file-change">${media.fileChangesSummaryMarkup([{ path: textPath, name: 'extract_conversation.py', op: 'edit' }])}</div>`;
  }, { textPath: TEXT_PATH });
  await page.waitForTimeout(100);
  return { context, page, errors };
}

const css = (page, selector, prop) => page.evaluate(
  ([sel, name]) => {
    const el = document.querySelector(sel);
    return el ? getComputedStyle(el)[name] : null;
  }, [selector, prop]);

(async () => {
  const server = spawn(PY, ['-m', 'http.server', String(PORT), '--directory', path.join(ROOT, 'public')], {
    cwd: ROOT, windowsHide: true,
  });
  server.stderr.on('data', (d) => console.error(String(d).trim()));
  let code = 1;
  try {
    await waitForServer();
    const browser = await chromium.launch({ channel: 'msedge', headless: true });

    // ══ A. 桌面形态 ══
    const desk = await preparePage(browser, { desktop: true });
    const page = desk.page;
    const bars = await page.locator('[data-file-actions]').count();
    check('① 两类 chip 旁都挂上了动作条（非媒体 file-chip + 本轮修改文件 chip）',
      bars === 2, `count=${bars}`);
    const actions = await page.locator('[data-smoke="file-chip"] [data-file-actions] button:not([data-file-more])').count();
    check('② 动作条里是「打开 / 打开文件夹 / 另存为」三个动作',
      actions === 3, `count=${actions}`);
    check('③ 「打开文件夹」确实渲染了（桌面端专属动作，非 pywebview 环境见 B 段负向断言）',
      (await page.locator('[data-file-reveal]').count()) === 2,
      `count=${await page.locator('[data-file-reveal]').count()}`);
    check('④ 桌面不出现「⋯」（悬停替代；两套入口同时出现会让 chip 变宽）',
      (await css(page, '[data-file-more]', 'display')) === 'none',
      await css(page, '[data-file-more]', 'display'));
    check('⑤ 未悬停时动作条零占位（chip 独占整格，版式与改前一致）',
      (await css(page, '[data-smoke="file-chip"] [data-file-actions]', 'display')) === 'none',
      await css(page, '[data-smoke="file-chip"] [data-file-actions]', 'display'));
    await page.hover('[data-smoke="file-chip"] .file-chip');
    await page.waitForTimeout(120);
    check('⑥ 悬停后动作条露出',
      (await css(page, '[data-smoke="file-chip"] [data-file-actions]', 'display')) === 'flex',
      await css(page, '[data-smoke="file-chip"] [data-file-actions]', 'display'));
    // 图标 paint：SVG 默认 fill:rgb(0,0,0) / stroke:none，只给尺寸会画成实心块
    // （插话键就是这么错的，§九.135 第 6 项）。这条必须在"动作条真的看得见"之后测。
    const iconPaint = await page.evaluate(() => {
      const svg = document.querySelector('[data-smoke="file-chip"] .file-action svg');
      const style = getComputedStyle(svg);
      return { fill: style.fill, stroke: style.stroke, w: svg.getBoundingClientRect().width };
    });
    check('⑦ 动作图标真的画得出来（fill:none + stroke 有色 + 有尺寸）',
      iconPaint.fill === 'none' && iconPaint.stroke !== 'none' && iconPaint.w > 0,
      JSON.stringify(iconPaint));
    check('⑧ 负向：没有「分享给好友」「添加到对话框」（本期明确不做，防范围蔓延）',
      (await page.locator('text=分享给好友').count()) === 0
      && (await page.locator('text=添加到对话框').count()) === 0
      && (await page.locator('[data-file-share], [data-file-to-composer]').count()) === 0);

    const clickAction = async (selector, index = 0) => {
      await page.locator(selector).nth(index).click({ force: true });
      await page.waitForTimeout(120);
      return page.evaluate(() => window.__bridgeCalls.slice());
    };
    let calls = await clickAction('[data-smoke="file-chip"] [data-file-open]');
    check('⑨ 点「打开」过桥调 naibaOpenFile，参数就是该文件的绝对路径',
      calls.length === 1 && calls[0][0] === 'naibaOpenFile' && calls[0][1] === TEXT_PATH,
      JSON.stringify(calls));
    await page.evaluate(() => { window.__bridgeCalls = []; });
    calls = await clickAction('[data-smoke="file-chip"] [data-file-reveal]');
    check('⑩ 点「打开文件夹」过桥调 naibaRevealInFolder',
      calls.length === 1 && calls[0][0] === 'naibaRevealInFolder' && calls[0][1] === TEXT_PATH,
      JSON.stringify(calls));
    await page.evaluate(() => { window.__bridgeCalls = []; });
    // 动作条是「悬停自己那一行才露出」：换行点之前必须先把它悬出来，
    // 否则拿到的是上一行的可见态（force:true 也救不了 display:none 的零尺寸盒）。
    await page.hover('[data-smoke="file-change"] .file-change-chip');
    await page.waitForTimeout(120);
    calls = await clickAction('[data-smoke="file-change"] [data-file-save-as]');
    check('⑪ 点「另存为」过桥调 naibaSaveFileAs，并把默认文件名一起带走（桌面不走 <a download>）',
      calls.length === 1 && calls[0][0] === 'naibaSaveFileAs'
      && calls[0][1] === TEXT_PATH && calls[0][2] === CHIP_NAME,
      JSON.stringify(calls));
    check('⑫ 点动作条不会顺带打开右侧文件面板（chip 本体行为没被抢走）',
      (await page.evaluate(() => window.__m.panel.filePanelState.open)) === false,
      JSON.stringify(await page.evaluate(() => window.__m.panel.filePanelState.open)));
    check('⑬ chip 本体仍是「打开文件面板」那条链（data-open-file 与实际路径没变）',
      (await page.locator('[data-smoke="file-change"] [data-open-file]').count()) === 1
      && (await page.getAttribute('[data-smoke="file-change"] [data-open-file]', 'data-open-file')) === TEXT_PATH);
    check('⑭ 桌面零 pageerror / 零 console.error（404 噪音除外）',
      desk.errors.length === 0, desk.errors.join(' | '));
    await desk.context.close();

    // ══ B. 手机形态（无 pywebview）══
    const mob = await preparePage(browser, { desktop: false });
    const m = mob.page;
    check('⑮ 手机端「打开文件夹」根本不渲染（不是 CSS 藏起来——点了只会在 PC 上弹窗口）',
      (await m.locator('[data-file-reveal]').count()) === 0,
      `count=${await m.locator('[data-file-reveal]').count()}`);
    check('⑯ 三个动作按钮在手机上收起来（无悬停可言）',
      (await css(m, '[data-smoke="file-chip"] [data-file-open]', 'display')) === 'none'
      && (await css(m, '[data-smoke="file-chip"] [data-file-save-as]', 'display')) === 'none',
      `${await css(m, '[data-smoke="file-chip"] [data-file-open]', 'display')} / ${await css(m, '[data-smoke="file-chip"] [data-file-save-as]', 'display')}`);
    const moreBox = await m.evaluate(() => {
      const el = document.querySelector('[data-smoke="file-chip"] [data-file-more]');
      const box = el.getBoundingClientRect();
      return { w: Math.round(box.width), h: Math.round(box.height), display: getComputedStyle(el).display };
    });
    check('⑰ 「⋯」常驻且过 44px 最小点按尺寸',
      moreBox.display === 'grid' && moreBox.w >= 44 && moreBox.h >= 44, JSON.stringify(moreBox));

    await m.locator('[data-smoke="file-chip"] [data-file-more]').click();
    await m.waitForTimeout(150);
    const menu = await m.evaluate(() => {
      const el = document.querySelector('#fileActionMenu');
      if (!el || el.hidden) return { open: false };
      const box = el.getBoundingClientRect();
      return {
        open: true,
        items: [...el.querySelectorAll('button')].map((b) => b.textContent.trim()),
        onScreen: box.left >= 0 && box.right <= window.innerWidth + 1 && box.top >= 0,
        role: el.getAttribute('role'),
      };
    });
    check('⑱ 点「⋯」弹出动作菜单，菜单项正好是「打开 / 另存为」两项',
      menu.open && menu.items.length === 2
      && menu.items[0] === '打开' && menu.items[1] === '另存为',
      JSON.stringify(menu));
    check('⑲ 菜单完整落在 360px 屏内（右侧对齐后会贴边，不许溢出）',
      menu.open && menu.onScreen && menu.role === 'menu', JSON.stringify(menu));

    await m.evaluate(() => { window.__downloads = []; });
    await m.locator('#fileActionMenu button').nth(1).click();  // 另存为
    await m.waitForTimeout(200);
    const downloads = await m.evaluate(() => window.__downloads);
    const href = downloads[0]?.href || '';
    check('⑳ 手机「另存为」复用第 3 项的下载链（同源 + download=1 + 文件名中文保真）',
      downloads.length === 1 && /[?&]download=1(&|$)/.test(href) && href.includes('/api/file')
      && downloads[0].name === CHIP_NAME,
      JSON.stringify(downloads));
    check('㉑ 点完菜单项菜单自动收起',
      (await m.evaluate(() => document.querySelector('#fileActionMenu').hidden)) === true);

    await m.evaluate(() => { window.__opened = []; });
    await m.locator('[data-smoke="file-change"] [data-file-more]').click();
    await m.waitForTimeout(150);
    await m.locator('#fileActionMenu button').nth(0).click();  // 打开
    await m.waitForTimeout(200);
    const opened = await m.evaluate(() => window.__opened);
    // 传进来的是同源相对地址（`/api/file?...`），解析时必须给 base，否则 new URL 直接抛。
    const openedPath = opened.length === 1 ? new URL(opened[0], BASE).searchParams.get('path') : null;
    check('㉒ 手机「打开」= /api/file 新标签预览（不假装能唤起本机程序）',
      opened.length === 1 && opened[0].includes('/api/file')
      && decodeURIComponent(openedPath || '') === TEXT_PATH,
      JSON.stringify(opened));
    await m.evaluate(() => { window.__downloads = []; });
    await m.keyboard.press('Escape');
    await m.locator('[data-smoke="file-chip"] [data-file-more]').click();
    await m.waitForTimeout(120);
    await m.mouse.click(4, 4);
    await m.waitForTimeout(120);
    check('㉓ 点别处菜单自动收起（不挡住下面的消息）',
      (await m.evaluate(() => document.querySelector('#fileActionMenu').hidden)) === true);
    check('㉔ 手机零 pageerror / 零 console.error（404 噪音除外）',
      mob.errors.length === 0, mob.errors.join(' | '));
    await mob.context.close();

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
