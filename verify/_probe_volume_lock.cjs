// 音量滑条「锁死」取证探针（纯只读，不改任何状态：不点击、不 POST、不落库）。
//
// 取证目标（对应《计划-音量拖条与提示音锁死.md》假设表 H1/H4）：
//   1. 页面所有 input[type=range]：disabled / pointer-events / elementsFromPoint 全栈
//      —— 若有透明覆盖层压在滑条上，栈顶元素会直接点名。
//   2. 所有 <video>：原生控件音量键/播放键区域的 elementsFromPoint 全栈 + readyState。
//   3. dialog[open]（含 :modal）、activeElement、3×3 视口网格顶元素（抓全屏透明层）。
//
// 用法一（隔离实例）：
//   NAIBA_TMP_PORT=8801 .venv\Scripts\python.exe verify\_serve_tmp.py
//   node verify\_probe_volume_lock.cjs
// 用法二（直连本人正在锁死的真人实例，需已开局域网访问）：
//   node verify\_probe_volume_lock.cjs http://192.168.5.x:PORT
//
// 输出：verify\_probe_volume_lock.out.json（中文不进 stdout）。
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || `http://127.0.0.1:${Number(process.env.NAIBA_TMP_PORT || 8801)}`;
const OUT = path.join(__dirname, '_probe_volume_lock.out.json');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 页内取证：serialize 后全是纯数据（tag/id/class/几何），不带节点引用。
const AUDIT = () => {
  const tag = (el) => {
    if (!el) return null;
    const id = el.id ? `#${el.id}` : '';
    const cls = typeof el.className === 'string' && el.className ? `.${el.className.trim().split(/\s+/).join('.')}` : '';
    return `${el.tagName.toLowerCase()}${id}${cls}`;
  };
  const stackAt = (x, y) =>
    document.elementsFromPoint(x, y).map(tag);
  const auditRange = (el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    return {
      target: tag(el),
      disabled: el.disabled,
      readOnly: el.readOnly,
      pointerEvents: cs.pointerEvents,
      value: el.value,
      rect: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
      // 全栈：压在滑条中心点上的每一个元素（第 0 个 = 实际接事件的那个）
      stack: (cx > 0 && cy > 0 && cx < innerWidth && cy < innerHeight) ? stackAt(cx, cy) : ['(视口外，可能在折叠区/未滚到)'],
    };
  };
  const auditVideo = (v) => {
    const r = v.getBoundingClientRect();
    // 原生控件条在底部：音量键约在右缘内侧 ~70px，播放键在左缘 ~40px（Chromium 桌面布局近似值）
    const vy = r.bottom - 18;
    return {
      target: tag(v),
      readyState: v.readyState,
      paused: v.paused,
      rect: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
      volumeAreaStack: r.width > 0 ? stackAt(r.right - 70, vy) : ['(尺寸为 0)'],
      playAreaStack: r.width > 0 ? stackAt(r.left + 40, vy) : ['(尺寸为 0)'],
    };
  };
  const grid = [];
  for (const fy of [0.1, 0.5, 0.9]) {
    for (const fx of [0.1, 0.5, 0.9]) {
      grid.push({ point: `${Math.round(innerWidth * fx)},${Math.round(innerHeight * fy)}`, top: stackAt(innerWidth * fx, innerHeight * fy).slice(0, 3) });
    }
  }
  return {
    url: location.href,
    viewport: { w: innerWidth, h: innerHeight },
    activeElement: tag(document.activeElement),
    dialogs: [...document.querySelectorAll('dialog')].map((d) => ({ target: tag(d), open: d.open, modal: d.matches(':modal') })),
    ranges: [...document.querySelectorAll('input[type=range]')].map(auditRange),
    videos: [...document.querySelectorAll('video')].map(auditVideo),
    gridTopElements: grid,
  };
};

async function main() {
  // 探活：后端必须可达，否则给出明确起法提示（放 main 顶层，不在文件头写 try/catch）。
  try {
    const probe = await fetch(`${BASE}/api/bootstrap`, { signal: AbortSignal.timeout(4000) });
    console.log(`backend /api/bootstrap -> ${probe.status}`);
  } catch (e) {
    console.error(`backend unreachable (${BASE}): ${e.message}`);
    console.error('start isolated instance first: NAIBA_TMP_PORT=8801 .venv\\Scripts\\python.exe verify\\_serve_tmp.py');
    process.exit(2);
  }

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1584, height: 1067 } });
  page.on('pageerror', (err) => console.log(`[pageerror] ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') console.log(`[console.error] ${msg.text().slice(0, 200)}`); });
  // 抓所有 >=400 的响应：定位「下方小字错误」到底是哪个接口在报
  const badResponses = [];
  page.on('response', (resp) => {
    if (resp.status() >= 400) badResponses.push({ status: resp.status(), url: resp.url().slice(0, 160) });
  });

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await sleep(2500); // 等 bootstrap/首屏渲染

  const report = { base: BASE, steps: [] };

  // 第一步：主页面（聊天态）取证 —— 视频卡在消息流里，原生控件区直接量。
  report.steps.push({ step: 'main-page', data: await page.evaluate(AUDIT) });

  // 第二步：打开设置对话框再取证（#doneSoundVolume 只在设置里存在）。
  // 只在探针自己的浏览器会话里开，不影响真人窗口；不点任何保存类按钮。
  const openResult = await page.evaluate(() => {
    const dlg = document.querySelector('#settingsDialog');
    if (!dlg) return { opened: false, reason: 'no #settingsDialog' };
    if (!dlg.open) dlg.showModal();
    return { opened: true };
  });
  await sleep(1200); // 等设置页渲染完
  report.steps.push({ step: 'settings-open', openResult, data: await page.evaluate(AUDIT) });

  // 第三步（行为级复现）：真拖 #doneSoundVolume——先滚进视口，鼠标按住滑块右移 80px，
  // 记录拖动前后的 value 与页面上出现的 toast 文案。对照组：同法拖 #chatFontSize。
  // （只写隔离实例 / 探针自己会话的状态，不碰真人数据。）
  async function dragRange(selector, dx) {
    const handle = page.locator(selector);
    await handle.scrollIntoViewIfNeeded();
    await sleep(400);
    const before = await handle.inputValue().catch(() => null);
    const box = await handle.boundingBox();
    if (!box) return { selector, error: 'no bounding box' };
    const startX = box.x + box.width * 0.25;
    const startY = box.y + box.height / 2;
    await page.mouse.move(startX, startY);
    await page.mouse.down();
    for (let i = 1; i <= 8; i++) await page.mouse.move(startX + (dx * i) / 8, startY);
    await page.mouse.up();
    await sleep(600);
    const after = await handle.inputValue().catch(() => null);
    const toast = await page.evaluate(() => {
      const el = document.querySelector('#toast');
      return el ? { hidden: el.hidden, text: (el.textContent || '').slice(0, 120) } : null;
    });
    return { selector, before, after, moved: before !== after, toast };
  }
  report.dragDoneSound = await dragRange('#doneSoundVolume', 80);
  report.dragChatFontSize = await dragRange('#chatFontSize', 60);
  report.badResponses = badResponses;

  await browser.close();
  fs.writeFileSync(OUT, JSON.stringify(report, null, 2), 'utf8');
  console.log(`written: ${OUT}`);
  console.log(`ranges=${report.steps[0].data.ranges.length}/${report.steps[1].data.ranges.length} videos=${report.steps[0].data.videos.length}/${report.steps[1].data.videos.length}`);
}

main().catch((err) => {
  console.error(`probe failed: ${err.message}`);
  process.exit(1);
});
