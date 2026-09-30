// @ / 弹层 × 软键盘（visualViewport）冒烟：手机视口下弹层不得被顶出可视屏幕。
//
// 缺陷前提（2026-09-30 手机截图报障）：Android Chrome 弹软键盘时布局视口不动、可视视口
// 缩小且被推到输入框上方（vv.offsetTop > 0）；弹层 position:fixed 只拿布局坐标、重定位只挂
// window.resize ⇒ 弹层上半截（面包屑头 + 前几行）被顶到可视屏幕之外。
//
// 真键盘无法在 Playwright 里模拟 ⇒ 用 Object.defineProperty 桩掉 window.visualViewport
// （height 压到 300、offsetTop = 输入框底 - height，即「键盘顶边贴住输入框底」的真实几何），
// 再在**真对象**上派发 resize/scroll（bootstrap 绑的就是真对象，重定位函数读到的已是桩）。
//
// 断言口径（先证明缺陷前提，再验证修复）：
//   桩前：@ 弹层以 CSS max-height（300px）满高贴输入框上方（记录 preRect）；
//   桩后：preRect.top < 桩可视区顶（复现「弹层顶部 < 可视区顶」＝未修复时面包屑头必被裁），
//         且修复后弹层矩形完整落在桩可视区内、面包屑头可见、内联 maxHeight 生效；
//   桌面：无 visualViewport 偏移时，/ 与 @ 弹层位置与旧数学逐像素一致（回归口径）。
//
// 自带隔离实例：自动 spawn verify/_serve_tmp.py（全新 NAIBA_TMP_ROOT + 端口 8817），跑完即杀。
// 产出落在 verify/_popup_kbd_raw/。
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const PORT = 8817;
const BASE = `http://127.0.0.1:${PORT}`;
const OUT = path.join(__dirname, '_popup_kbd_raw');
const SERVER_ROOT = path.join(__dirname, '_popup_kbd_root');
const WS = path.join(SERVER_ROOT, 'ws');
const PYTHON = path.join(__dirname, '..', '.venv', 'Scripts', 'python.exe');

// 桩几何：手机键盘弹出后可视高度（键盘顶边贴输入框底）。300 比「砍半」更能复现
// 截图里「面包屑头 + 前几行被裁」——844 高的屏可视区只剩输入框上方那一条。
const VV_HEIGHT = 300;

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? `  ${detail}` : ''}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const near = (a, b, eps = 0.6) => Math.abs(a - b) <= eps;

function prepareServerRoot() {
  fs.rmSync(SERVER_ROOT, { recursive: true, force: true });
  fs.mkdirSync(path.join(WS, 'docs', 'api'), { recursive: true });
  // 12 个条目：保证弹层内容高超过 CSS max-height（300px），量出来的才是「被约束的高」。
  for (let i = 1; i <= 10; i++) fs.writeFileSync(path.join(WS, `素材批次${String(i).padStart(2, '0')}.md`), 'x');
  fs.writeFileSync(path.join(WS, 'docs', 'guide.md'), 'guide');
  fs.writeFileSync(path.join(WS, 'docs', 'api', 'README.md'), 'readme');
}

async function startServer() {
  const child = spawn(PYTHON, [path.join(__dirname, '_serve_tmp.py')], {
    env: { ...process.env, NAIBA_TMP_PORT: String(PORT), NAIBA_TMP_ROOT: SERVER_ROOT, PYTHONIOENCODING: 'utf-8' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '';
  child.stdout.on('data', (d) => { log += d; });
  child.stderr.on('data', (d) => { log += d; });
  childlogs.push({ child, tail: () => log.slice(-2000) });
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(BASE);
      if (res.ok) return child;
    } catch { /* not ready yet */ }
    await sleep(300);
  }
  console.error(`服务未在 30s 内就绪，日志尾：\n${log.slice(-2000)}`);
  child.kill();
  process.exit(1);
}
const childlogs = [];

async function stripOverlays(page) {
  await page.evaluate(() => {
    document.getElementById('onboardingDialog')?.remove();
    document.getElementById('authDialog')?.remove();
  });
}

// 打开 @ 弹层并等它渲染完（懒加载目录浏览是异步的）。
async function openFilePopup(page) {
  await page.fill('#messageInput', '@');
  await page.waitForSelector('#filePopup:not([hidden]) .file-popup-item', { timeout: 15000 });
  await sleep(400);
}

// 弹层几何快照。
async function popupRect(page, sel) {
  return page.evaluate((s) => {
    const popup = document.querySelector(s);
    const r = popup.getBoundingClientRect();
    const head = popup.querySelector('.file-popup-crumb') || popup.querySelector('button, .skill-popup-empty');
    const hr = head ? head.getBoundingClientRect() : null;
    return {
      top: r.top, bottom: r.bottom, left: r.left, right: r.right, height: r.height,
      headTop: hr ? hr.top : null, headBottom: hr ? hr.bottom : null,
      inlineMaxHeight: popup.style.maxHeight || '',
    };
  }, sel);
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  prepareServerRoot();
  const server = await startServer();

  // 播种冒烟会话（带工作区）：@ 弹层的目录浏览挂在会话工作区上（沿用 file_ref_smoke 口径）。
  const created = await fetch(`${BASE}/api/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: '弹层键盘冒烟', workspace_dir: WS }),
  }).then((r) => r.json());
  if (!created.id) {
    console.error(`会话创建失败：${JSON.stringify(created).slice(0, 300)}`);
    server.kill();
    process.exit(1);
  }

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    // ===== Part A：手机视口（390×844）=====
    console.log('== A. 手机视口：软键盘（visualViewport 桩）弹出后弹层必须完整落在可视区内');
    const mobile = await browser.newContext({
      viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
      deviceScaleFactor: 2,
    });
    const page = await mobile.newPage();
    const pageErrors = [];
    page.on('pageerror', (err) => pageErrors.push(String(err)));
    await page.goto(BASE, { waitUntil: 'load', timeout: 20000 });
    await stripOverlays(page);
    await page.waitForSelector('#messageInput', { timeout: 15000 });
    await sleep(800);

    // A1 桩前：键盘未弹（真 visualViewport，全高）。弹层满高贴输入框上方。
    await openFilePopup(page);
    const pre = await popupRect(page, '#filePopup');
    check('桩前 @ 弹层打开且满高（命中 CSS max-height 300）', near(pre.height, 300, 1.5),
      `height=${pre.height.toFixed(1)}`);
    const inputTop = await page.evaluate(() => document.querySelector('#messageInput').getBoundingClientRect().top);
    check('桩前弹层贴输入框上方（旧数学 top = input.top - ph - 6）',
      near(pre.top, inputTop - pre.height - 6, 1), `top=${pre.top.toFixed(1)} 期望 ${inputTop - pre.height - 6}`);
    check('桩前弹层在（键盘未弹的）可视区内', pre.top >= 0 && pre.bottom <= 844,
      `[${pre.top.toFixed(1)}, ${pre.bottom.toFixed(1)}]`);
    await page.screenshot({ path: path.join(OUT, '01-mobile-idle-popup.png') });

    // A2 装桩：height=300、offsetTop=输入框底-300（键盘顶边贴住输入框底的真实几何）。
    const stubTop = await page.evaluate((h) => {
      const real = window.visualViewport;
      const rect = document.querySelector('#messageInput').getBoundingClientRect();
      const offsetTop = Math.max(0, Math.round(rect.bottom - h));
      const stub = {
        height: h, width: window.innerWidth, offsetTop, pageTop: 0, scale: 1,
        onresize: null, onscroll: null,
      };
      Object.defineProperty(window, 'visualViewport', { configurable: true, value: stub });
      window.__vvStub = stub;
      window.__vvReal = real; // bootstrap 绑定的是真对象：事件要派发到它身上
      return offsetTop;
    }, VV_HEIGHT);
    const stubBottom = stubTop + VV_HEIGHT;
    check('桩几何成立（offsetTop > 0 且可视区小于布局视口）',
      stubTop > 0 && VV_HEIGHT < 844, `可视区=[${stubTop}, ${stubBottom}]，布局视口 844`);

    // A3 缺陷前提：桩前弹层顶部低于桩可视区顶 ⇒ 未修复时面包屑头必被裁（先证明前提成立）。
    check('缺陷前提成立：桩前弹层顶部 < 可视区顶（未修复时头部被裁）',
      pre.top < stubTop, `pre.top=${pre.top.toFixed(1)} < vvTop=${stubTop}，被裁 ${(stubTop - pre.top).toFixed(0)}px`);
    check('缺陷前提成立：桩前面包屑头整段在可视区顶之外', pre.headBottom !== null && pre.headBottom < stubTop,
      `headBottom=${pre.headBottom?.toFixed(1)} < vvTop=${stubTop}`);

    // A4 派发 resize（真对象）→ 重定位函数读到桩几何。
    await page.evaluate(() => { window.__vvReal.dispatchEvent(new Event('resize')); });
    await sleep(200);
    const fixed = await popupRect(page, '#filePopup');
    check('桩后弹层顶部回到可视区内', fixed.top >= stubTop, `top=${fixed.top.toFixed(1)} ≥ vvTop=${stubTop}`);
    check('桩后弹层底部不出可视区', fixed.bottom <= stubBottom,
      `bottom=${fixed.bottom.toFixed(1)} ≤ vvBottom=${stubBottom}`);
    check('桩后面包屑头可见（head 落在可视区内）',
      fixed.headTop >= stubTop && fixed.headBottom <= stubBottom,
      `head=[${fixed.headTop?.toFixed(1)}, ${fixed.headBottom?.toFixed(1)}]`);
    const above = inputTop - stubTop;
    check('桩后内联 maxHeight 压进可用空间（above-6，且 ≤ CSS 300）',
      fixed.inlineMaxHeight && parseFloat(fixed.inlineMaxHeight) <= above - 6 + 0.6
        && parseFloat(fixed.inlineMaxHeight) <= 300.6,
      `maxHeight=${fixed.inlineMaxHeight}，above=${above.toFixed(1)}`);
    await page.screenshot({ path: path.join(OUT, '02-mobile-keyboard-open-fixed.png') });

    // A5 键盘收合动画（scroll 事件）：可视区向上长 60px，弹层必须跟着重排。
    await page.evaluate(() => {
      window.__vvStub.offsetTop -= 60;
      window.__vvStub.height += 60;
      window.__vvReal.dispatchEvent(new Event('scroll'));
    });
    await sleep(200);
    const afterScroll = await popupRect(page, '#filePopup');
    check('vv scroll 后弹层仍完整落在（变大的）可视区内',
      afterScroll.top >= stubTop - 60 && afterScroll.bottom <= stubBottom,
      `top=${afterScroll.top.toFixed(1)} ≥ ${stubTop - 60}，bottom=${afterScroll.bottom.toFixed(1)} ≤ ${stubBottom}`);
    await page.screenshot({ path: path.join(OUT, '03-mobile-vv-scroll-follow.png') });

    // A6 键盘收起（删桩恢复真对象 + resize）：弹层回到满高旧位（约束可长回去）。
    await page.evaluate(() => {
      delete window.visualViewport; // configurable 桩：delete 还原原型上的真 getter
      window.__vvReal.dispatchEvent(new Event('resize'));
    });
    await sleep(200);
    const restored = await popupRect(page, '#filePopup');
    check('键盘收起后弹层恢复满高旧位（内联约束不残留）',
      near(restored.top, pre.top, 1.5) && near(restored.height, 300, 1.5),
      `top=${restored.top.toFixed(1)}（桩前 ${pre.top.toFixed(1)}），height=${restored.height.toFixed(1)}`);

    // A7 hide 清内联 maxHeight。
    await page.keyboard.press('Escape');
    await sleep(200);
    const cleared = await page.evaluate(() => {
      const popup = document.getElementById('filePopup');
      return { hidden: popup.hidden, inlineMaxHeight: popup.style.maxHeight || '' };
    });
    check('Esc 关闭后内联 maxHeight 已清（不污染下一轮）',
      cleared.hidden && cleared.inlineMaxHeight === '', JSON.stringify(cleared));

    // A8 同一函数服务 / Skill 弹层：键盘态下同样不得出可视区。
    await page.evaluate((h) => { // 重新装桩（A6 已删）
      const rect = document.querySelector('#messageInput').getBoundingClientRect();
      window.__vvStub = {
        height: h, width: window.innerWidth,
        offsetTop: Math.max(0, Math.round(rect.bottom - h)), pageTop: 0, scale: 1,
      };
      Object.defineProperty(window, 'visualViewport', { configurable: true, value: window.__vvStub });
      window.__vvReal.dispatchEvent(new Event('resize'));
    }, VV_HEIGHT);
    await page.fill('#messageInput', '/');
    await page.waitForSelector('#skillPopup:not([hidden])', { timeout: 15000 });
    await sleep(300);
    const skill = await popupRect(page, '#skillPopup');
    const vvTop2 = await page.evaluate(() => window.visualViewport.offsetTop);
    check('/ Skill 弹层（键盘态）完整落在可视区内',
      skill.top >= vvTop2 && skill.bottom <= vvTop2 + VV_HEIGHT,
      `[${skill.top.toFixed(1)}, ${skill.bottom.toFixed(1)}] vv=[${vvTop2}, ${vvTop2 + VV_HEIGHT}]`);
    await page.screenshot({ path: path.join(OUT, '04-mobile-skill-popup.png') });
    await page.keyboard.press('Escape');

    check('手机视口零页面异常', pageErrors.length === 0, pageErrors.join(' | '));
    await mobile.close();

    // ===== Part B：桌面回归（1440×900，无桩）=====
    console.log('== B. 桌面回归：无 visualViewport 偏移时弹层位置与现版逐像素一致');
    const desktop = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const dpage = await desktop.newPage();
    const dErrors = [];
    dpage.on('pageerror', (err) => dErrors.push(String(err)));
    await dpage.goto(BASE, { waitUntil: 'load', timeout: 20000 });
    await stripOverlays(dpage);
    await dpage.waitForSelector('#messageInput', { timeout: 15000 });
    await sleep(800);

    await openFilePopup(dpage);
    const dFile = await popupRect(dpage, '#filePopup');
    const dInputTop = await dpage.evaluate(() => document.querySelector('#messageInput').getBoundingClientRect().top);
    check('桌面 @ 弹层位置与旧数学一致（top = input.top - ph - 6）',
      near(dFile.top, dInputTop - dFile.height - 6, 1),
      `top=${dFile.top.toFixed(1)} 期望 ${dInputTop - dFile.height - 6}`);
    check('桌面 @ 弹层无内联压高（= CSS 上限 300）', near(parseFloat(dFile.inlineMaxHeight || '300'), 300, 0.6),
      `inlineMaxHeight=${dFile.inlineMaxHeight || '(空=CSS 300)'}`);
    await dpage.screenshot({ path: path.join(OUT, '05-desktop-file-popup.png') });

    await dpage.fill('#messageInput', '');
    await dpage.fill('#messageInput', '/');
    await dpage.waitForSelector('#skillPopup:not([hidden])', { timeout: 15000 });
    await sleep(300);
    const dSkill = await popupRect(dpage, '#skillPopup');
    const dSkillInputTop = await dpage.evaluate(() => document.querySelector('#messageInput').getBoundingClientRect().top);
    check('桌面 / 弹层位置与旧数学一致（top = input.top - ph - 6）',
      near(dSkill.top, dSkillInputTop - dSkill.height - 6, 1),
      `top=${dSkill.top.toFixed(1)} 期望 ${dSkillInputTop - dSkill.height - 6}`);
    await dpage.screenshot({ path: path.join(OUT, '06-desktop-skill-popup.png') });
    check('桌面零页面异常', dErrors.length === 0, dErrors.join(' | '));
    await desktop.close();
  } finally {
    await browser.close();
    server.kill();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n==== ${results.length - failed.length}/${results.length} 通过 ====`);
  console.log(failed.length ? `结果：${failed.length} 项失败` : '结果：全部通过');
  process.exit(failed.length ? 1 : 0);
}

main().catch((err) => {
  console.error(err);
  for (const c of childlogs) console.error(`--- server log tail ---\n${c.tail()}`);
  process.exit(1);
});
