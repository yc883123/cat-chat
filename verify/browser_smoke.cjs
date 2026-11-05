// 浏览器基础冒烟：桌面 + 375px 手机视口加载 http://127.0.0.1:8765/（可用 NAIBA_SMOKE_BASE 覆盖端口）。
// 断言：标题为 `Cat Chat`；侧栏/消息区存在；输入框与发送按钮在视口内且仍属于聊天主网格；
// 无 pageerror / console.error / JS ERROR / PROMISE REJECT 警示条。任一缺失即失败（不能只记录不判定）。
// 运行：$env:NODE_PATH="%USERPROFILE%\node_modules"; node verify\browser_smoke.cjs
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8765';
const OUTPUT = path.resolve(process.env.NAIBA_SMOKE_OUTPUT || __dirname);
fs.mkdirSync(OUTPUT, { recursive: true });

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile', width: 375, height: 812 },
];

async function runViewport(browser, vp) {
  const errors = [];
  const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  // 浏览器会自己去请求 /favicon.ico，而源码实例没有这条路由（HTTP 层 404）。
  // 那是浏览器的资源噪音、不是应用错误：按 URL 排除，其余 4xx/5xx 仍照旧判红。
  page.on('response', (response) => {
    if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) {
      errors.push(`HTTP ${response.status()} ${response.url()}`);
    }
  });
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    // 「Failed to load resource」由上面的 response 监听按 URL 统计，这里只收应用自身打印的错误。
    if (/Failed to load resource/.test(msg.text())) return;
    errors.push(`console.error: ${msg.text()}`);
  });
  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForTimeout(3500);
    // 应用会恢复上次停留的顶层视图；用户可能上次停在工作台，
    // 这时 #messages/#messageInput 仍在 DOM 中但被整个对话视图隐藏。
    // 基础冒烟验证的是对话壳本身，先显式切回「对话」再检查，避免把
    // 合法的视图恢复状态误报成前端缺失。
    const chatMode = page.locator('#openChatMode');
    if (await chatMode.count() && !(await chatMode.getAttribute('aria-pressed'))?.toLowerCase().includes('true')) {
      await chatMode.click();
      await page.waitForTimeout(700);
    }
    const state = await page.evaluate(() => {
      // 「可见」= 已渲染（存在、非 display:none、非 visibility:hidden、有非零盒子）。
      // 手机端侧栏是 position:fixed + translateX(-102%) 的离屏抽屉，computed visibility
      // 仍为 visible、盒子非零，故按此判据仍算已渲染（是否滑入视口由打开按钮控制）。
      const rendered = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
      };
      const insideViewport = (sel) => {
        const el = document.querySelector(sel);
        if (!el || !rendered(sel)) return false;
        const r = el.getBoundingClientRect();
        return r.top >= -1 && r.left >= -1 && r.bottom <= innerHeight + 1 && r.right <= innerWidth + 1;
      };
      const body = document.body.innerText || '';
      return {
        title: document.title,
        jsError: body.includes('JS ERROR'),
        promiseReject: body.includes('PROMISE REJECT'),
        sidebar: rendered('#sidebar'),
        messages: rendered('#messages'),
        messageInput: insideViewport('#messageInput'),
        sendButton: insideViewport('#sendButton'),
        composerInMain: document.querySelector('#composerForm')?.closest('.composer-wrap')?.parentElement === document.querySelector('.chat-main'),
        mainInShell: document.querySelector('.chat-main')?.parentElement === document.querySelector('#appShell'),
      };
    });
    console.log(`[${vp.name}] DOM 状态:`, JSON.stringify(state));
    if (state.title !== 'Cat Chat') errors.push(`标题不是 Cat Chat（实际 ${JSON.stringify(state.title)}）`);
    if (!state.sidebar) errors.push('侧栏 #sidebar 缺失或不可见');
    if (!state.messages) errors.push('消息区 #messages 缺失或不可见');
    if (!state.messageInput) errors.push('输入控件 #messageInput 缺失或不可见');
    if (!state.sendButton) errors.push('发送按钮 #sendButton 缺失或不在视口内');
    if (!state.composerInMain || !state.mainInShell) errors.push('聊天输入区脱离主网格或主网格脱离应用容器');
    if (state.jsError) errors.push('DOM 存在 JS ERROR 警示条');
    if (state.promiseReject) errors.push('DOM 存在 PROMISE REJECT 警示条');
    await page.screenshot({ path: path.join(OUTPUT, `browser_smoke_${vp.name}.png`), fullPage: false });
  } catch (e) {
    errors.push(`加载失败: ${e.message}`);
    await page.screenshot({ path: path.join(OUTPUT, `browser_smoke_failed_${vp.name}.png`) }).catch(() => {});
  }
  await page.close();
  return errors;
}

(async () => {
  const browser = await chromium.launch({ channel: process.env.NAIBA_SMOKE_BROWSER_CHANNEL || 'msedge', headless: true });
  const all = [];
  try {
    for (const vp of VIEWPORTS) {
      const errs = await runViewport(browser, vp);
      errs.forEach((e) => all.push(`[${vp.name}] ${e}`));
    }
  } finally { await browser.close(); }
  if (all.length) {
    console.log('❌ 浏览器冒烟失败：');
    all.forEach((e) => console.log('  ' + e));
    process.exit(1);
  }
  console.log('✅ 浏览器冒烟通过：桌面 + 375px 手机；标题 Cat Chat，侧栏/消息区/输入控件齐全且可见，零页面错误');
})().catch(error => { console.error(error); process.exitCode = 1; });
