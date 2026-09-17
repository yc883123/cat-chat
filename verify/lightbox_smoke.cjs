// 大图灯箱左右切换冒烟（源码 server，端口 8799）。
// 运行：$env:NODE_PATH="%USERPROFILE%\node_modules"; node verify\lightbox_smoke.cjs
const zlib = require('zlib');
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8799';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  return response.json().catch(() => ({}));
}

// 最小 PNG 生成（不同尺寸/颜色 → 内容哈希不同，避免上传去重）
function png(width, height, rgb) {
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    const rowStart = y * (width * 3 + 1);
    raw[rowStart] = 0;
    for (let x = 0; x < width; x += 1) {
      const off = rowStart + 1 + x * 3;
      raw[off] = rgb[0];
      raw[off + 1] = rgb[1];
      raw[off + 2] = rgb[2];
    }
  }
  const chunk = (type, data) => {
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    const typeBuf = Buffer.from(type, 'ascii');
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(zlib.crc32(Buffer.concat([typeBuf, data])) >>> 0);
    return Buffer.concat([length, typeBuf, data, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 2; // truecolor
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

async function upload(name, bytes) {
  const boundary = `----lightboxsmoke${Date.now()}${Math.random().toString(16).slice(2)}`;
  const body = Buffer.concat([
    Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${name}"\r\nContent-Type: image/png\r\n\r\n`, 'utf-8'),
    bytes,
    Buffer.from(`\r\n--${boundary}--\r\n`, 'utf-8'),
  ]);
  const response = await fetch(`${BASE}/api/uploads`, {
    method: 'POST',
    headers: { 'Content-Type': `multipart/form-data; boundary=${boundary}` },
    body,
  });
  return response.json();
}

async function waitRunDone(conversationId, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const active = await apiJson(`/api/runs?conversation_id=${encodeURIComponent(conversationId)}&active_only=1`);
    if (!(active.runs || []).length) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

async function sendTurn(conversationId, attachment, text) {
  const response = await fetch(`${BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      conversation_id: conversationId,
      message: text,
      display_message: text,
      attachments: [attachment],
    }),
  });
  await response.body?.cancel?.().catch(() => {});
  return waitRunDone(conversationId);
}

function pathOf(url) {
  try {
    return decodeURIComponent(new URL(url, BASE).searchParams.get('path') || '');
  } catch (_) {
    return '';
  }
}

(async () => {
  // 准备 3 张尺寸/颜色不同的图片并上传
  const uploaded = [];
  const specs = [
    // A 用大图（缩放后大于视口，才能验证平移）；B/C 用小图
    { name: '灯箱A.png', w: 1600, h: 1200, rgb: [200, 60, 60] },
    { name: '灯箱B.png', w: 230, h: 161, rgb: [60, 160, 90] },
    { name: '灯箱C.png', w: 250, h: 171, rgb: [70, 90, 210] },
  ];
  for (const spec of specs) {
    const result = await upload(spec.name, png(spec.w, spec.h, spec.rgb));
    uploaded.push({ name: spec.name, path: result.path, thumb: result.thumb_path });
  }
  check('3 张图片已上传', uploaded.every((item) => Boolean(item.path)), JSON.stringify(uploaded.map((i) => i.path)));

  const created = await apiJson('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({ title: '灯箱冒烟' }),
  });
  const conversationId = String(created.id || '');
  check('冒烟会话已创建', Boolean(conversationId), JSON.stringify(created).slice(0, 120));
  for (let i = 0; i < uploaded.length; i += 1) {
    // 与真实前端一致：附件带 thumb_path，避免派生缩略图路径 404。
    const ok = await sendTurn(
      conversationId,
      { name: uploaded[i].name, path: uploaded[i].path, thumb_path: uploaded[i].thumb },
      `第 ${i + 1} 张图`,
    );
    check(`第 ${i + 1} 轮已发送`, ok, 'run 未结束');
  }

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const notFound = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('response', (res) => { if (res.status() === 404) notFound.push(res.url()); });

  const lightbox = () => page.evaluate(() => {
    const box = document.querySelector('#imageLightbox');
    const img = document.querySelector('#imageLightboxImg');
    const prev = document.querySelector('#imageLightboxPrev');
    const next = document.querySelector('#imageLightboxNext');
    const counter = document.querySelector('#imageLightboxCounter');
    return {
      open: box ? box.hidden === false : false,
      src: img ? (img.getAttribute('src') || '') : '',
      prevHidden: prev ? prev.hidden : null,
      nextHidden: next ? next.hidden : null,
      counter: counter ? counter.textContent : '',
      counterHidden: counter ? counter.hidden : null,
    };
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messages .attachment-image img[data-large-url]', { timeout: 20000 });
    await page.waitForTimeout(1200);

    const thumbs = await page.evaluate(() =>
      [...document.querySelectorAll('#messages img[data-large-url]')].map((el) => el.getAttribute('data-large-url')));
    check('会话内渲染 3 张图片', thumbs.length === 3, JSON.stringify(thumbs.map(pathOf)));

    // ① 点击第 2 张 → 灯箱打开、计数 2/3、左右按钮可见
    await page.click('#messages img[data-large-url] >> nth=1');
    await page.waitForTimeout(500);
    let state = await lightbox();
    check('灯箱已打开', state.open, JSON.stringify(state));
    check('计数为 2 / 3', state.counter === '2 / 3', JSON.stringify(state));
    check('左右按钮可见', state.prevHidden === false && state.nextHidden === false, JSON.stringify(state));
    check('显示第 2 张图', pathOf(state.src) === uploaded[1].path, `${pathOf(state.src)} vs ${uploaded[1].path}`);

    // ② 下一张 → 3/3；再下一张 → 回到 1/3（循环）
    await page.click('#imageLightboxNext');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('点击 → 到第 3 张', state.counter === '3 / 3' && pathOf(state.src) === uploaded[2].path, JSON.stringify(state));
    await page.click('#imageLightboxNext');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('继续 → 循环回第 1 张', state.counter === '1 / 3' && pathOf(state.src) === uploaded[0].path, JSON.stringify(state));

    // ③ 键盘 ← / → 切换
    await page.keyboard.press('ArrowLeft');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('← 回到第 3 张', state.counter === '3 / 3' && pathOf(state.src) === uploaded[2].path, JSON.stringify(state));
    await page.keyboard.press('ArrowLeft');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('← 到第 2 张', state.counter === '2 / 3', JSON.stringify(state));
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('→ 回到第 3 张', state.counter === '3 / 3', JSON.stringify(state));

    // ④ 点击左侧按钮（上一张）
    await page.click('#imageLightboxPrev');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('点击 ← 按钮到第 2 张', state.counter === '2 / 3', JSON.stringify(state));

    // ⑤ Esc 关闭
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('Esc 关闭灯箱', state.open === false, JSON.stringify(state));

    // ⑤b 半屏点击翻页（重新打开）
    await page.click('#messages img[data-large-url] >> nth=1');
    await page.waitForTimeout(400);
    check('重新打开在 2 / 3', (await lightbox()).counter === '2 / 3', JSON.stringify(await lightbox()));
    await page.mouse.click(120, 450);
    await page.waitForTimeout(300);
    state = await lightbox();
    check('点击左半屏 → 上一张', state.counter === '1 / 3' && pathOf(state.src) === uploaded[0].path, JSON.stringify(state));
    await page.mouse.click(1280 - 120, 450);
    await page.waitForTimeout(300);
    state = await lightbox();
    check('点击右半屏 → 下一张', state.counter === '2 / 3' && pathOf(state.src) === uploaded[1].path, JSON.stringify(state));

    // ⑤c 缩放 / 拖动（用第 1 张大图：缩放后大于视口才能验证平移）
    await page.mouse.click(120, 450);
    await page.waitForTimeout(300);
    state = await lightbox();
    check('回到第 1 张大图', state.counter === '1 / 3', JSON.stringify(state));

    await page.mouse.move(640, 450);
    await page.mouse.wheel(0, -300);
    await page.waitForTimeout(350);
    const zoomState = () => page.evaluate(() => {
      const img = document.querySelector('#imageLightboxImg');
      const box = document.querySelector('#imageLightbox');
      const match = /scale\(([\d.]+)\)/.exec(img.style.transform || '');
      return {
        scale: match ? Number(match[1]) : 1,
        transform: img.style.transform || '',
        isZoomed: box.classList.contains('is-zoomed'),
        rect: (() => { const r = img.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; })(),
      };
    });
    let zoom = await zoomState();
    check('滚轮放大', zoom.scale > 1.05, JSON.stringify(zoom));
    check('缩放态标记 is-zoomed', zoom.isZoomed === true, JSON.stringify(zoom));
    check('缩放后图片大于视口', zoom.rect[0] > 1280, JSON.stringify(zoom.rect));

    await page.mouse.click(120, 450);
    await page.waitForTimeout(300);
    state = await lightbox();
    check('缩放后单击不翻页', state.counter === '1 / 3', JSON.stringify(state));

    const beforeDrag = (await zoomState()).transform;
    await page.mouse.move(640, 450);
    await page.mouse.down();
    await page.mouse.move(760, 510, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(250);
    const afterDrag = (await zoomState()).transform;
    check('拖动平移改变位移', beforeDrag !== afterDrag, `${beforeDrag} -> ${afterDrag}`);

    await page.mouse.dblclick(640, 450);
    await page.waitForTimeout(300);
    zoom = await zoomState();
    check('双击复位缩放', zoom.scale === 1 && zoom.transform === '' && zoom.isZoomed === false, JSON.stringify(zoom));

    // ⑤d 换图后缩放复位
    await page.mouse.move(640, 450);
    await page.mouse.wheel(0, -300);
    await page.waitForTimeout(300);
    await page.click('#imageLightboxNext');
    await page.waitForTimeout(300);
    zoom = await zoomState();
    check('换图后缩放复位', zoom.scale === 1 && zoom.transform === '', JSON.stringify(zoom));
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    // ⑥ 输入区待发送附件：单张，不显示左右按钮（每次运行内容不同，避免命中去重）
    await page.setInputFiles('#fileInput', {
      name: '灯箱D.png',
      mimeType: 'image/png',
      buffer: png(180, 140, [120, 120, 100 + (Date.now() % 100)]),
    });
    await page.waitForSelector('#pendingFiles .file-chip img[data-large-url]', { timeout: 20000 });
    await page.waitForTimeout(600);
    await page.click('#pendingFiles img[data-large-url]');
    await page.waitForTimeout(400);
    state = await lightbox();
    check('输入区图片也能打开灯箱', state.open, JSON.stringify(state));
    check('单张图不显示左右按钮', state.prevHidden === true && state.nextHidden === true, JSON.stringify(state));
    check('单张图不显示计数', state.counterHidden === true, JSON.stringify(state));
    // 单张图：点击空白处关闭（点击图片本身不关闭）
    await page.mouse.click(120, 450);
    await page.waitForTimeout(300);
    check('单张图点击空白处关闭', (await lightbox()).open === false, JSON.stringify(await lightbox()));

    // ⑦ 手机形态（390x844）：灯箱控件必须浮在图片之上，且关闭按钮/计数不压在图片上。
    //    回归背景（用户实测）：img 带 will-change:transform 会自建层叠上下文（层级视为 0），
    //    而它在 DOM 里排在 close/prev 之后，按文档顺序绘制会盖住左箭头——手机上表现为
    //    「左箭头被图片吞掉、点击穿透成半屏翻页」，而右箭头正常，左右不一致。
    //    这里临时取消手机形态的上下安全带并把图片撑满视口（最极端条件），对每个控件做 hitTest。
    await page.setViewportSize({ width: 390, height: 844 });
    await page.click('#messages img[data-large-url] >> nth=0');
    await page.waitForTimeout(500);
    // 手机形态的上下「控件安全带」：叉叉与计数应完整落在图片之外，不再骑在图片边缘上
    const mobileLayout = await page.evaluate(() => {
      const pick = (sel) => {
        const r = document.querySelector(sel).getBoundingClientRect();
        return { top: Math.round(r.top), bottom: Math.round(r.bottom), width: Math.round(r.width) };
      };
      return { img: pick('#imageLightboxImg'), close: pick('#imageLightboxClose'), counter: pick('#imageLightboxCounter'), prev: pick('#imageLightboxPrev'), next: pick('#imageLightboxNext') };
    });
    check('手机形态：关闭按钮不与图片重叠', mobileLayout.close.bottom <= mobileLayout.img.top, JSON.stringify(mobileLayout));
    check('手机形态：计数不与图片重叠', mobileLayout.counter.top >= mobileLayout.img.bottom, JSON.stringify(mobileLayout));
    check('手机形态：左右箭头尺寸一致', mobileLayout.prev.width === mobileLayout.next.width && mobileLayout.prev.top === mobileLayout.next.top, JSON.stringify(mobileLayout));
    await page.addStyleTag({ content: '.image-lightbox{padding:24px!important}.image-lightbox img{max-width:94vw!important;max-height:94vh!important}' });
    await page.evaluate(() => {
      const img = document.querySelector('#imageLightboxImg');
      img.style.width = '94vw';
      img.style.height = '94vh';
      img.style.objectFit = 'cover';
    });
    const hitControl = (selector) => page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (!el || el.hidden) return { ok: false, reason: 'hidden' };
      const rect = el.getBoundingClientRect();
      const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return { ok: Boolean(top && (top === el || el.contains(top))), reason: top ? (top.id || top.tagName.toLowerCase()) : 'null' };
    }, selector);
    for (const [label, selector] of [
      ['左箭头', '#imageLightboxPrev'],
      ['右箭头', '#imageLightboxNext'],
      ['关闭按钮', '#imageLightboxClose'],
      ['计数', '#imageLightboxCounter'],
    ]) {
      const result = await hitControl(selector);
      check(`手机形态：${label}浮在图片之上（未被遮挡）`, result.ok, JSON.stringify(result));
    }
    const navGeometry = await page.evaluate(() => {
      const pick = (sel) => {
        const r = document.querySelector(sel).getBoundingClientRect();
        return [Math.round(r.top), Math.round(r.width), Math.round(r.height)];
      };
      return { prev: pick('#imageLightboxPrev'), next: pick('#imageLightboxNext') };
    });
    check('手机形态：左右箭头尺寸与纵向位置一致', JSON.stringify(navGeometry.prev) === JSON.stringify(navGeometry.next), JSON.stringify(navGeometry));
    await page.click('#imageLightboxPrev');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('手机形态：左箭头可点击翻页（回到最后一张）', state.counter === '3 / 3', JSON.stringify(state));
    await page.screenshot({ path: `${__dirname}\\lightbox_mobile_smoke.png` });
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    check('零页面错误', pageErrors.length === 0, pageErrors.join(' | '));
    console.log('  404 资源:', notFound.length ? JSON.stringify([...new Set(notFound)]) : '无');
    console.log('  上传结果:', JSON.stringify(uploaded));
    await page.screenshot({ path: `${__dirname}\\lightbox_smoke.png` });
  } catch (error) {
    failures.push(`执行异常: ${error.message}`);
    console.log('执行异常:', error.message);
  }

  await browser.close();
  if (conversationId) await apiJson(`/api/conversations/${conversationId}`, { method: 'DELETE' });

  console.log('');
  console.log('灯箱切换冒烟结果：', failures.length ? `${failures.length} 项失败 -> ${JSON.stringify(failures)}` : '全部通过');
  process.exit(failures.length ? 1 : 0);
})();
