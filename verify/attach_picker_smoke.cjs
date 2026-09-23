// 「手机上也能传文档」前端守门（静态 public/ 服务，无需后端）——§九.135 第 4 项。
//
// 缺陷形态：`#fileInput` 不带 accept，点附件键直接 `input.click()`。无 accept 时"手机浏览器
// 给什么入口"因设备/ROM 而异——部分 Android 直接进相册，用户**根本看不到「文件」**，
// 于是"传个 PDF/Word 给 AI 看"在手机上变成不可能。后端这条路一直是通的
// （`store_uploaded_file` 只卡 80MB，不做类型白名单），缺的只是入口。
//
// 本冒烟钉住「入口」这一半，两种形态各跑一遍：
//   手机：附件键先弹「照片 / 视频 · 文件」两项；两项分别给对应的 accept；**不新建第二个
//        输入框**（上传链路只能有一条）；选完真的走到"待发送区出现 chip"这一端到端结果；
//   桌面：行为与改前逐字节一致——不弹动作条、直接开选择器、accept 复位成空。
//
// 用法：$env:NODE_PATH="<node_modules>"; node verify\attach_picker_smoke.cjs
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.NAIBA_ATTACH_SMOKE_PORT || 8820);
const BASE = `http://127.0.0.1:${PORT}`;
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');

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

async function preparePage(browser, { mobile }) {
  const context = await browser.newContext(mobile
    ? { viewport: { width: 360, height: 780 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true }
    : { viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });
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
  // 记下"到底点开了哪个 input"——只看 accept 不够：accept 对了但没点开，用户还是看不到选择器。
  await page.addInitScript(() => {
    window.__pickerCalls = [];
    const original = HTMLInputElement.prototype.click;
    HTMLInputElement.prototype.click = function patchedClick() {
      if (this.type === 'file') window.__pickerCalls.push({ id: this.id, accept: this.accept });
      return original.call(this);
    };
  });
  await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
  await page.waitForSelector('#attachButton', { timeout: 15000 });
  await page.waitForTimeout(300);
  await page.evaluate(() => { document.querySelectorAll('dialog[open]').forEach((d) => d.close()); });
  return { context, page, errors };
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

    // ══ A. 手机形态 ══
    const mob = await preparePage(browser, { mobile: true });
    const m = mob.page;
    check('① 输入区只有一条上传链路（表单里就一个 type=file 的 input）',
      (await m.locator('#composerForm input[type=file]').count()) === 1,
      `count=${await m.locator('#composerForm input[type=file]').count()}`);

    await m.locator('#attachButton').click();
    await m.waitForTimeout(150);
    const menu = await m.evaluate(() => {
      const el = document.querySelector('#attachMenu');
      if (!el || el.hidden) return { open: false };
      const box = el.getBoundingClientRect();
      const itemBoxes = [...el.querySelectorAll('button')].map((b) => {
        const r = b.getBoundingClientRect();
        return { text: b.textContent.trim(), h: Math.round(r.height) };
      });
      const trigger = document.querySelector('#attachButton').getBoundingClientRect();
      return {
        open: true,
        items: itemBoxes,
        onScreen: box.left >= 0 && box.right <= window.innerWidth + 1 && box.top >= 0 && box.bottom <= window.innerHeight + 1,
        aboveTrigger: Math.round(box.bottom) <= Math.round(trigger.top) + 1,
        pickerCalls: window.__pickerCalls.length,
      };
    });
    check('② 手机点附件键先弹两项入口（不再直接扎进相册）',
      menu.open && menu.items.length === 2, JSON.stringify(menu));
    check('③ 两项文案说得清是什么：照片 / 视频 · 文件',
      menu.open && menu.items[0].text.includes('照片') && menu.items[1].text.includes('文件'),
      JSON.stringify(menu.open ? menu.items.map((i) => i.text) : null));
    check('④ 动作条在按钮**上方**展开（附件键在屏幕最下方，向下弹一定被切掉）',
      menu.open && menu.aboveTrigger, JSON.stringify(menu));
    check('⑤ 动作条完整落在 360px 屏内',
      menu.open && menu.onScreen, JSON.stringify(menu));
    check('⑥ 菜单项过 44px 最小点按尺寸',
      menu.open && menu.items.every((i) => i.h >= 44), JSON.stringify(menu.open ? menu.items : null));
    check('⑦ 弹菜单本身不打开任何选择器（只是问一句，还没到挑文件）',
      menu.open && menu.pickerCalls === 0, JSON.stringify(menu));

    await m.locator('#attachMenu button').nth(0).click();  // 照片 / 视频
    await m.waitForTimeout(150);
    let calls = await m.evaluate(() => window.__pickerCalls);
    check('⑧ 选「照片 / 视频」→ 开的是同一个 #fileInput，accept 限定图片/视频',
      calls.length === 1 && calls[0].id === 'fileInput' && calls[0].accept === 'image/*,video/*',
      JSON.stringify(calls));
    check('⑨ 选完自动收起',
      (await m.evaluate(() => document.querySelector('#attachMenu').hidden)) === true);

    await m.evaluate(() => { window.__pickerCalls = []; });
    await m.locator('#attachButton').click();
    await m.waitForTimeout(120);
    await m.locator('#attachMenu button').nth(1).click();  // 文件
    await m.waitForTimeout(150);
    calls = await m.evaluate(() => window.__pickerCalls);
    const accept = calls[0]?.accept || '';
    check('⑩ 选「文件」→ accept 换成文档清单（.pdf/.docx/.md…都有），强制系统出文件管理器',
      calls.length === 1 && calls[0].id === 'fileInput'
      && ['.pdf', '.docx', '.md', '.txt', '.zip'].every((ext) => accept.includes(ext)),
      accept);
    check('⑪ accept 非空：留空在部分 ROM 上又会被当成"随便选"而回到相册（本轮修的正是这条路）',
      accept.length > 0 && !accept.startsWith('image/'), accept);
    // ⑪-1/⑪-2 是用户实测「点了文件后还是跳相册」之后的补强：纯扩展名清单会被部分 ROM 忽略、
    // 回落 `*/*` 从而把相册摆在第一个。判据是「给了 MIME」且「MIME 段里没有多媒体」——
    // 这两条同时成立，系统才会把相册/相机过滤掉。只断言"非空"抓不到这个回退。
    check('⑪-1 「文件」的 accept 里给了 MIME 类型（application/… 或 text/…），不只是后缀',
      /application\//.test(accept) && /text\//.test(accept), accept.slice(0, 120));
    check('⑪-2 「文件」的 accept 里不含 image/* 与 video/*（含了就等于把相册又请回来）',
      !accept.includes('image/') && !accept.includes('video/'), accept.slice(0, 120));

    // 端到端：选完文件真的进到"待发送区出现 chip"。上传本身会 404（静态服务没有 /api/uploads），
    // chip 是被移除后 toast 报错的——所以断言必须在**同一个同步任务里**读完（这正是产品真实时序：
    // chip 先出现，随后才被上传结果覆盖）。
    const chipText = await m.evaluate(() => {
      const file = new File(['hello'], '需求说明.pdf', { type: 'application/pdf' });
      const dt = new DataTransfer();
      dt.items.add(file);
      const input = document.querySelector('#fileInput');
      input.files = dt.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      const box = document.querySelector('#pendingFiles');
      return { hidden: box.hidden, text: box.textContent.trim() };
    });
    check('⑫ 选中的文件真的进了待发送区（change → uploadFiles → chip，端到端）',
      chipText.hidden === false && chipText.text.includes('需求说明.pdf'), JSON.stringify(chipText));

    await m.keyboard.press('Escape');
    await m.locator('#attachButton').click();
    await m.waitForTimeout(120);
    await m.mouse.click(4, 4);
    await m.waitForTimeout(120);
    check('⑬ 点别处动作条自动收起（不挡住输入区）',
      (await m.evaluate(() => document.querySelector('#attachMenu').hidden)) === true);
    check('⑭ 手机零 pageerror / 零 console.error（404 噪音除外）',
      mob.errors.length === 0, mob.errors.join(' | '));
    await mob.context.close();

    // ══ B. 桌面形态：行为与改前逐字节一致 ══
    const desk = await preparePage(browser, { mobile: false });
    const d = desk.page;
    await d.locator('#attachButton').click();
    await d.waitForTimeout(150);
    const deskState = await d.evaluate(() => ({
      menuOpen: Boolean(document.querySelector('#attachMenu') && !document.querySelector('#attachMenu').hidden),
      calls: window.__pickerCalls,
    }));
    check('⑮ 桌面点附件键：不弹动作条，直接开文件选择器（与改前一致）',
      deskState.menuOpen === false && deskState.calls.length === 1
      && deskState.calls[0].id === 'fileInput', JSON.stringify(deskState));
    check('⑯ 桌面传进来的 accept 是空串（不偷偷给桌面加限制，选择器与改前完全一样）',
      deskState.calls[0]?.accept === '', JSON.stringify(deskState.calls[0]?.accept));
    check('⑰ 桌面零 pageerror / 零 console.error（404 噪音除外）',
      desk.errors.length === 0, desk.errors.join(' | '));
    await desk.context.close();

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
