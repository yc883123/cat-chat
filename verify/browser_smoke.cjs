// 浏览器基础冒烟：桌面 + 375px 手机视口加载 http://127.0.0.1:8765/（可用 NAIBA_SMOKE_BASE 覆盖端口）。
// 断言：标题为 `Cat Chat`；侧栏 #sidebar、消息区 #messages、输入控件 #messageInput 存在且可见；
// 无 pageerror / console.error / JS ERROR / PROMISE REJECT 警示条。任一缺失即失败（不能只记录不判定）。
// 运行：$env:NODE_PATH="%USERPROFILE%\node_modules"; node verify\browser_smoke.cjs
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8765';

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile', width: 375, height: 812 },
];

async function runViewport(browser, vp) {
  const errors = [];
  const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`);
  });
  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForTimeout(3500);
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
      const body = document.body.innerText || '';
      return {
        title: document.title,
        jsError: body.includes('JS ERROR'),
        promiseReject: body.includes('PROMISE REJECT'),
        sidebar: rendered('#sidebar'),
        messages: rendered('#messages'),
        messageInput: rendered('#messageInput'),
      };
    });
    console.log(`[${vp.name}] DOM 状态:`, JSON.stringify(state));
    if (state.title !== 'Cat Chat') errors.push(`标题不是 Cat Chat（实际 ${JSON.stringify(state.title)}）`);
    if (!state.sidebar) errors.push('侧栏 #sidebar 缺失或不可见');
    if (!state.messages) errors.push('消息区 #messages 缺失或不可见');
    if (!state.messageInput) errors.push('输入控件 #messageInput 缺失或不可见');
    if (state.jsError) errors.push('DOM 存在 JS ERROR 警示条');
    if (state.promiseReject) errors.push('DOM 存在 PROMISE REJECT 警示条');
    await page.screenshot({ path: path.join(__dirname, `browser_smoke_${vp.name}.png`), fullPage: false });
  } catch (e) {
    errors.push(`加载失败: ${e.message}`);
  }
  await page.close();
  return errors;
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const all = [];
  for (const vp of VIEWPORTS) {
    const errs = await runViewport(browser, vp);
    errs.forEach((e) => all.push(`[${vp.name}] ${e}`));
  }
  await browser.close();
  if (all.length) {
    console.log('❌ 浏览器冒烟失败：');
    all.forEach((e) => console.log('  ' + e));
    process.exit(1);
  }
  console.log('✅ 浏览器冒烟通过：桌面 + 375px 手机；标题 Cat Chat，侧栏/消息区/输入控件齐全且可见，零页面错误');
})();
