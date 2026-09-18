// 冒烟：会话区字体（设置 → 外观 → 会话字体）——面板控件 → CSS 变量 → 消息正文 全链路。
// 前置：python verify/chat_font_smoke.py（由它播种 + 起隔离源码实例 + 跑本脚本）
// 运行：$env:NODE_PATH="<node_modules>"; node verify\chat_font_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8798';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

// 消息正文的计算样式 + :root 变量：一次拿全，避免多次 round-trip 之间状态漂移。
async function fontSnapshot(page) {
  return page.evaluate(() => {
    const body = document.querySelector('.message-row.assistant .message-body')
      || document.querySelector('.message-body');
    const root = document.documentElement;
    const style = body ? getComputedStyle(body) : null;
    return {
      size: style ? Math.round(parseFloat(style.fontSize) * 100) / 100 : -1,
      family: style ? style.fontFamily : '',
      varSize: root.style.getPropertyValue('--msg-font-size').trim(),
      varFamily: root.style.getPropertyValue('--msg-font-family').trim(),
      stored: localStorage.getItem('naibaChatAppearance') || '',
    };
  });
}

async function panelSnapshot(page) {
  return page.evaluate(() => {
    const slider = document.querySelector('#chatFontSize');
    const output = document.querySelector('#chatFontSizeValue');
    const custom = document.querySelector('#chatFontFamilyCustom');
    const row = document.querySelector('#chatFontCustomRow');
    const pick = document.querySelector('#chatFontPick');
    const manualRow = document.querySelector('#chatFontManualRow');
    const checked = document.querySelector('input[name="appearanceChatFont"]:checked');
    const sample = document.querySelector('#chatFontSample');
    return {
      hasSlider: Boolean(slider),
      sliderValue: slider ? slider.value : '',
      output: output ? output.textContent.trim() : '',
      family: checked ? checked.value : '',
      customHidden: row ? row.hidden : null,
      customValue: custom ? custom.value : '',
      pickValue: pick ? pick.value : '',
      pickOptions: pick ? [...pick.options].map((o) => o.textContent.trim()) : [],
      manualHidden: manualRow ? manualRow.hidden : null,
      sampleSize: sample ? Math.round(parseFloat(getComputedStyle(sample).fontSize) * 100) / 100 : -1,
      fontStatus: (document.querySelector('#chatFontStatus') || {}).textContent || '',
    };
  });
}

// 独立复算「（未安装）」标注：从 JS 常量拿探测名，在本页重新 canvas 度量一次，
// 与 DOM 里那条 option 的文案逐项比对。只查 DOM 文案等于自己验自己；只查常量同理。
async function crossCheckPickAnnotations(page) {
  return page.evaluate(async () => {
    const mod = await import('/js/01-core.js');
    const pick = document.querySelector('#chatFontPick');
    const text = 'mmmmmmmmmmlli 永字八法';
    const ctx = document.createElement('canvas').getContext('2d');
    const widthOf = (family) => { ctx.font = `72px ${family}`; return ctx.measureText(text).width; };
    const baselines = ['monospace', 'sans-serif', 'serif'];
    const installed = (name) => (!name ? true : baselines.some(
      (b) => Math.abs(widthOf(`"${name}", ${b}`) - widthOf(b)) > 0.5,
    ));
    const rows = mod.CHAT_FONT_PICKS.map((item, index) => {
      const option = pick.options[index] || { value: '', textContent: '' };
      return {
        key: item.key,
        probe: item.probe,
        orderOk: option.value === item.key,
        annotated: option.textContent.includes('（未安装）'),
        expected: !installed(item.probe),
      };
    });
    return {
      rows,
      orderMismatch: rows.filter((r) => !r.orderOk).map((r) => r.key),
      labelMismatch: rows.filter((r) => r.annotated !== r.expected).map((r) => r.key),
      uninstalled: rows.filter((r) => r.annotated).map((r) => r.key),
    };
  });
}

const SERIF_PROBE = 'Source Han Serif SC';

// 每次拖动前重新取坐标：卡片里的状态文字/自定义框显隐会改变弹窗高度，
// 中心定位的 `<dialog>` 会整体上下移动——沿用旧坐标就会点到别处（实测踩过）。
async function dragSlider(page, edge) {
  const box = await page.locator('#chatFontSize').boundingBox();
  const x = edge === 'min' ? box.x + 2 : box.x + box.width - 2;
  await page.mouse.click(x, box.y + box.height / 2);
  await page.waitForTimeout(320);
}

async function readServerAppearance(page) {
  // 设置接口只有 POST（GET /api/settings 404）：空 body = 只读回显，不改任何值。
  return page.evaluate(async () => {
    const response = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const payload = await response.json();
    return (payload.settings || payload).appearance || {};
  });
}

async function openSettings(page) {
  // 幂等：弹窗已经开着时别再点一次——第二次点击会先去找侧栏按钮，
  // 而侧栏被已打开的 <dialog> 挡住，click 会一直重试到 30s 超时（实测踩过）。
  const alreadyOpen = await page.evaluate(
    () => document.querySelector('#settingsDialog')?.open === true,
  );
  if (!alreadyOpen) {
    const settingsBtn = page.locator('#openSettings');
    // 窄屏（≤760px）顶栏是两行网格 + overflow:hidden，侧栏头的设置按钮被挤出视口
    // ——移动端的真实路径是「☰ 打开侧栏抽屉 → 点设置」。用「是否落在视口内」判断，
    // 不看 isVisible()（被裁切时它仍返回 true，会一直点到 30s 超时）。
    const box = await settingsBtn.boundingBox().catch(() => null);
    const size = page.viewportSize();
    const inViewport = Boolean(box) && box.x >= 0 && box.y >= 0
      && box.x + box.width <= size.width && box.y + box.height <= size.height;
    if (!inViewport) {
      const sidebarBtn = page.locator('#openSidebar');
      if (await sidebarBtn.isVisible().catch(() => false)) {
        await sidebarBtn.click();
        await page.waitForTimeout(600);
      }
    }
    await page.click('#openSettings');
    await page.waitForTimeout(500);
  }
  const navTab = page.locator('.settings-nav button[data-settings-tab="appearance"]');
  if (await navTab.isVisible().catch(() => false)) await navTab.click();
  await page.waitForSelector('#chatFontSize', { timeout: 10000 });
  await page.waitForTimeout(250);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  const badResponses = [];
  const posts = [];
  // 永不清空的副本：用来断言「整轮从头到尾都没把 __pick__ 发出去」
  // （posts 每段检查前会被清空，只看它只能覆盖最后一批）。
  const allPosts = [];
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`); });
  page.on('response', (res) => {
    if (res.status() >= 400) badResponses.push(`${res.status()} ${res.request().method()} ${res.url()}`);
  });
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes('/api/settings')) {
      posts.push(req.postData() || '');
      allPosts.push(req.postData() || '');
    }
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForSelector('.message-row.assistant .message-body', { timeout: 20000 });
    await page.waitForTimeout(800);

    // ---- 1. 默认值：与历史硬编码完全等价（老用户视觉零变化） ----
    let snap = await fontSnapshot(page);
    check('默认正文字号 = 15px', snap.size === 15, JSON.stringify({ size: snap.size }));
    check('默认正文用界面字体（--font-sans 的 Inter 栈）',
      snap.family.includes('Inter') && snap.varSize === '15px' && snap.varFamily === 'var(--font-sans)',
      JSON.stringify({ family: snap.family, varSize: snap.varSize, varFamily: snap.varFamily }));

    // ---- 2. 新插入的节点自动同字号（流式增量渲染无需额外处理） ----
    const fresh = await page.evaluate(() => {
      const host = document.querySelector('#messages');
      const probe = document.createElement('div');
      probe.className = 'message-body';
      probe.textContent = '流式新节点探针';
      host.appendChild(probe);
      const size = Math.round(parseFloat(getComputedStyle(probe).fontSize) * 100) / 100;
      probe.remove();
      return size;
    });
    check('新插入的消息节点自动继承同一字号（流式期一致）', fresh === 15, String(fresh));

    // ---- 3. 打开设置 → 外观 → 会话字体卡片 ----
    await openSettings(page);
    let panel = await panelSnapshot(page);
    check('卡片回显当前值（滑块 15 / 15px / 跟随界面 / 自定义框收起）',
      panel.hasSlider && panel.sliderValue === '15' && panel.output === '15px'
      && panel.family === 'system' && panel.customHidden === true,
      JSON.stringify(panel));

    // ---- 4. 真拖滑块 → 即时预览，且不落库 ----
    posts.length = 0;
    await dragSlider(page, 'max');
    panel = await panelSnapshot(page);
    snap = await fontSnapshot(page);
    check('拖到最右 → 18px（滑块 / 数值 / 样例 / 真实消息四处一致）',
      panel.sliderValue === '18' && panel.output === '18px' && panel.sampleSize === 18 && snap.size === 18,
      JSON.stringify({ panel: panel.sliderValue, output: panel.output, sample: panel.sampleSize, body: snap.size }));
    check('拖动过程不落库（0 次 POST /api/settings）', posts.length === 0, `posts=${posts.length}`);
    check('未保存时给出提示（两行状态）',
      panel.fontStatus.includes('未保存'), JSON.stringify({ fontStatus: panel.fontStatus }));

    await dragSlider(page, 'min');
    panel = await panelSnapshot(page);
    snap = await fontSnapshot(page);
    check('拖到最左 → 13px（下界可用）',
      panel.sliderValue === '13' && panel.output === '13px' && snap.size === 13,
      JSON.stringify({ panel: panel.sliderValue, body: snap.size }));
    await dragSlider(page, 'max');

    // ---- 5. 字体族：衬线 / 自定义 ----
    await page.click('label.appearance-option:has(input[value="serif"])');
    await page.waitForTimeout(300);
    snap = await fontSnapshot(page);
    panel = await panelSnapshot(page);
    check('选「衬线」→ 正文改用宋体栈',
      snap.family.includes(SERIF_PROBE) && panel.family === 'serif' && panel.customHidden === true,
      JSON.stringify({ family: snap.family }));
    // 光看声明不够（字体没装时声明照样在，渲染却回退到界面字体 = 静默失败）：
    // 用同一段文字量「界面字体 / 当前字体」的宽度，必须真的不同；再与候选字体的
    // canvas 度量对照，确认真的落在宋体上（而不是撞上了别的回退）。
    const metrics = await page.evaluate(() => {
      const text = '字体度量探针 Font metrics probe 0123456789';
      const probe = document.createElement('span');
      probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;font-size:16px';
      probe.textContent = text;
      document.body.appendChild(probe);
      probe.style.fontFamily = 'var(--font-sans)';
      const sans = probe.getBoundingClientRect().width;
      probe.style.fontFamily = 'var(--msg-font-family)';
      const current = probe.getBoundingClientRect().width;
      probe.remove();
      const ctx = document.createElement('canvas').getContext('2d');
      const measure = (family) => { ctx.font = `16px ${family}`; return ctx.measureText(text).width; };
      return {
        sans: Math.round(sans * 100) / 100,
        current: Math.round(current * 100) / 100,
        simsun: Math.round(measure('SimSun') * 100) / 100,
        yahei: Math.round(measure('"Microsoft YaHei"') * 100) / 100,
      };
    });
    check('衬线真的换了渲染字体（度量与界面字体不同）',
      Math.abs(metrics.current - metrics.sans) > 0.5, JSON.stringify(metrics));
    check('衬线落到宋体（度量与 SimSun 一致，不是别的回退）',
      Math.abs(metrics.current - metrics.simsun) <= Math.max(0.5, metrics.simsun * 0.02),
      JSON.stringify(metrics));

    // ---- 5b. 点选列表：radio「指定字体」→ select → 手动兜底 ----
    await page.click('label.appearance-option:has(input[value="__pick__"])');
    await page.waitForTimeout(300);
    panel = await panelSnapshot(page);
    check('选「指定字体」→ 展开下拉，手工行仍收起',
      panel.customHidden === false && panel.manualHidden === true && panel.pickOptions.length === 12,
      JSON.stringify({ customHidden: panel.customHidden, manualHidden: panel.manualHidden, n: panel.pickOptions.length }));
    check('下拉第 1 项是「微软雅黑」、末项是「手动填写…」（顺序 = 常量顺序）',
      panel.pickOptions[0].startsWith('微软雅黑') && panel.pickOptions[11].startsWith('手动填写'),
      JSON.stringify(panel.pickOptions));
    // 「（未安装）」标注必须与现场复算的度量逐项一致（含顺序、含探测名字段）。
    const cross = await crossCheckPickAnnotations(page);
    check('下拉项与 JS 常量一一对应（个数 / 顺序 / key）',
      cross.orderMismatch.length === 0, JSON.stringify(cross.orderMismatch));
    check('「（未安装）」标注与现场 canvas 度量逐项一致（不张冠李戴）',
      cross.labelMismatch.length === 0,
      JSON.stringify({ mismatch: cross.labelMismatch, uninstalled: cross.uninstalled }));
    check('缺失字体只标注不禁选（本机缺失项仍是可选项）',
      cross.rows.filter((r) => r.expected).every((r) => r.annotated),
      JSON.stringify(cross.uninstalled));

    // 点选一个 Windows 必装的预设：楷体
    await page.selectOption('#chatFontPick', 'kaiti');
    await page.waitForTimeout(400);
    snap = await fontSnapshot(page);
    panel = await panelSnapshot(page);
    check('点选「楷体」→ 正文改用楷体栈且带界面字体兜底',
      snap.family.includes('KaiTi') && panel.pickValue === 'kaiti' && panel.manualHidden === true,
      JSON.stringify({ family: snap.family, pick: panel.pickValue }));
    const kaiMetrics = await page.evaluate(() => {
      const text = '字体度量探针 Font metrics probe 0123456789';
      const probe = document.createElement('span');
      probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;font-size:16px';
      probe.textContent = text;
      document.body.appendChild(probe);
      probe.style.fontFamily = 'var(--font-sans)';
      const sans = probe.getBoundingClientRect().width;
      probe.style.fontFamily = 'var(--msg-font-family)';
      const current = probe.getBoundingClientRect().width;
      probe.remove();
      const ctx = document.createElement('canvas').getContext('2d');
      const measure = (family) => { ctx.font = `16px ${family}`; return ctx.measureText(text).width; };
      return {
        sans: Math.round(sans * 100) / 100,
        current: Math.round(current * 100) / 100,
        kaiti: Math.round(measure('KaiTi') * 100) / 100,
      };
    });
    check('楷体真的换了渲染字体（度量与界面字体不同）',
      Math.abs(kaiMetrics.current - kaiMetrics.sans) > 0.5, JSON.stringify(kaiMetrics));
    check('楷体落到楷体（度量与 KaiTi 一致，不是别的回退）',
      Math.abs(kaiMetrics.current - kaiMetrics.kaiti) <= Math.max(0.5, kaiMetrics.kaiti * 0.02),
      JSON.stringify(kaiMetrics));

    // 手动兜底：下拉最后一项 → 手工行展开 → 手敲字体名
    await page.selectOption('#chatFontPick', 'custom');
    await page.waitForTimeout(300);
    panel = await panelSnapshot(page);
    check('下拉选「手动填写…」→ 手工行展开', panel.manualHidden === false, JSON.stringify(panel));
    await page.click('#chatFontFamilyCustom');
    await page.keyboard.press('Control+A');
    await page.keyboard.type('LXGW WenKai');
    await page.waitForTimeout(400);
    snap = await fontSnapshot(page);
    check('手动字体名即时生效且带界面字体兜底',
      snap.family.includes('LXGW WenKai') && snap.family.includes('Inter') && snap.size === 18,
      JSON.stringify({ family: snap.family, size: snap.size }));

    // 回到预设「楷体」+ 18px 作为最终保存态
    await page.selectOption('#chatFontPick', 'kaiti');
    await page.waitForTimeout(250);

    // ---- 6. 保存 → 服务端持久化（预置键 / 手动串 两条路径都走一遍） ----
    posts.length = 0;
    await page.click('#saveAppearance');
    await page.waitForTimeout(900);
    const firstSaved = posts.length === 1 ? JSON.parse(posts[0]).appearance : null;
    check('点「保存外观」发出 1 次 POST：预置键落进 family（不是 __pick__、不是 custom）',
      Boolean(firstSaved) && firstSaved.chat_font_size === 18 && firstSaved.chat_font_family === 'kaiti',
      JSON.stringify({ count: posts.length, appearance: firstSaved }));
    const remote = await readServerAppearance(page);
    check('后端 /api/settings 回读一致（服务端是真相来源）',
      remote.chat_font_size === 18 && remote.chat_font_family === 'kaiti',
      JSON.stringify(remote));
    check('占位值 __pick__ 从未落库（整轮所有 POST 都不含它）',
      allPosts.length > 0 && allPosts.every((body) => !body.includes('__pick__')),
      JSON.stringify(allPosts.slice(-2)));

    // 换到「手动填写」再存一次，确认 custom 串能落库、且之后切回预置键不会清空它
    await page.click('label.appearance-option:has(input[value="__pick__"])');
    await page.waitForTimeout(250);
    await page.selectOption('#chatFontPick', 'custom');
    await page.waitForTimeout(250);
    await page.click('#chatFontFamilyCustom');
    await page.keyboard.press('Control+A');
    await page.keyboard.type('LXGW WenKai');
    await page.waitForTimeout(300);
    posts.length = 0;
    await page.click('#saveAppearance');
    await page.waitForTimeout(900);
    const customSaved = posts.length === 1 ? JSON.parse(posts[0]).appearance : null;
    check('选手动填写 → POST 带 family=custom 与手动串',
      Boolean(customSaved) && customSaved.chat_font_family === 'custom'
      && customSaved.chat_font_family_custom === 'LXGW WenKai',
      JSON.stringify({ count: posts.length, appearance: customSaved }));
    const customRemote = await readServerAppearance(page);
    check('后端回读手动串一致',
      customRemote.chat_font_family === 'custom' && customRemote.chat_font_family_custom === 'LXGW WenKai',
      JSON.stringify(customRemote));

    await page.selectOption('#chatFontPick', 'kaiti');
    await page.waitForTimeout(250);
    await page.click('#saveAppearance');
    await page.waitForTimeout(900);
    const backToPreset = await readServerAppearance(page);
    check('切回预置键后手动串仍在库里（点选不清空用户手敲的字）',
      backToPreset.chat_font_family === 'kaiti'
      && backToPreset.chat_font_family_custom === 'LXGW WenKai',
      JSON.stringify(backToPreset));

    // ---- 7. 换主题不重置字体（老调用点只传 theme/skin 的回归防线） ----
    await page.click('label.appearance-option:has(input[value="light"])');
    await page.waitForTimeout(300);
    panel = await panelSnapshot(page);
    snap = await fontSnapshot(page);
    check('只换主题 → 字号 18 / 楷体 保持不变（radio 仍在「指定字体」）',
      panel.sliderValue === '18' && panel.family === '__pick__' && panel.pickValue === 'kaiti'
      && snap.size === 18 && snap.family.includes('KaiTi'),
      JSON.stringify({ slider: panel.sliderValue, family: panel.family, pick: panel.pickValue, size: snap.size }));
    await page.click('#saveAppearance');
    await page.waitForTimeout(900);
    const afterTheme = await readServerAppearance(page);
    check('保存主题后字体键仍在库里',
      afterTheme.theme === 'light' && afterTheme.chat_font_size === 18 && afterTheme.chat_font_family === 'kaiti',
      JSON.stringify(afterTheme));

    // 证据截图：让「会话字体」卡片完整入镜（弹窗可滚动，卡片在下面）
    await page.evaluate(() => {
      const slider = document.querySelector('#chatFontSize');
      (slider ? slider.closest('.settings-card') : null)?.scrollIntoView({ block: 'center' });
    });
    await page.waitForTimeout(300);
    await page.screenshot({ path: 'verify/chat_font_smoke_1_settings.png' });
    await page.click('#settingsDialog button[data-close="settingsDialog"]');
    await page.waitForTimeout(400);
    await page.screenshot({ path: 'verify/chat_font_smoke_2_chat.png' });
    // 消息正文特写：改字体后字号/字体真的落到正文上的直接证据
    await page.locator('.message-row.assistant .message-body')
      .screenshot({ path: 'verify/chat_font_smoke_4_body_preset.png' });

    // ---- 8. 刷新：本地缓存保证首帧就对（不闪），清缓存后由服务端兜住 ----
    await page.reload({ waitUntil: 'domcontentloaded' });
    const firstTick = await page.evaluate(() => ({
      size: document.documentElement.style.getPropertyValue('--msg-font-size').trim(),
      family: document.documentElement.style.getPropertyValue('--msg-font-family').trim(),
      stored: localStorage.getItem('naibaChatAppearance') || '',
    }));
    check('刷新首帧就是保存值（localStorage 缓存，移动端不闪）',
      firstTick.size === '18px' && firstTick.family.includes('KaiTi')
      && firstTick.stored.includes('"chat_font_size":18'),
      JSON.stringify(firstTick));

    await page.waitForTimeout(1200);
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: 'load' });
    await page.waitForSelector('.message-row.assistant .message-body', { timeout: 20000 });
    await page.waitForTimeout(900);
    snap = await fontSnapshot(page);
    check('清掉本地缓存后刷新仍生效（证明服务端已持久化）',
      snap.size === 18 && snap.family.includes('KaiTi'), JSON.stringify({ size: snap.size, family: snap.family }));

    // 重启后面板要能把「预置键」反查回两级控件（radio=__pick__ + select=kaiti），
    // 而不是把 kaiti 当成未知值丢掉——这是"存得进、显示不出"的典型回归点。
    await openSettings(page);
    panel = await panelSnapshot(page);
    check('重启后面板回显预置键（radio=指定字体 / 下拉=楷体 / 手工行收起）',
      panel.family === '__pick__' && panel.pickValue === 'kaiti'
      && panel.customHidden === false && panel.manualHidden === true && panel.sliderValue === '18',
      JSON.stringify({ family: panel.family, pick: panel.pickValue, manual: panel.manualHidden }));

    // ---- 9. 手机竖屏：控件可达可操作、设置同样生效 ----
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(500);
    await openSettings(page);
    const mobile = await page.evaluate(() => {
      const slider = document.querySelector('#chatFontSize');
      slider.scrollIntoView({ block: 'center' });
      const rect = slider.getBoundingClientRect();
      const pick = document.querySelector('#chatFontPick');
      return {
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        inViewport: rect.top >= 0 && rect.bottom <= window.innerHeight && rect.left >= 0 && rect.right <= window.innerWidth,
        pickWidth: pick ? Math.round(pick.getBoundingClientRect().width) : 0,
      };
    });
    check('手机竖屏：滑块完整在视口内（够宽可拖）',
      mobile.width > 100 && mobile.inViewport === true, JSON.stringify(mobile));
    check('手机竖屏：下拉够宽可用（点选不靠打字）',
      mobile.pickWidth > 150, JSON.stringify(mobile));
    await dragSlider(page, 'min');
    panel = await panelSnapshot(page);
    snap = await fontSnapshot(page);
    check('手机竖屏：拖动即时生效（13px 生效到消息正文）',
      panel.sliderValue === '13' && snap.size === 13,
      JSON.stringify({ slider: panel.sliderValue, body: snap.size }));
    // 窄屏上点选一个预设字体也要真的换到正文
    await page.selectOption('#chatFontPick', 'yahei');
    await page.waitForTimeout(400);
    snap = await fontSnapshot(page);
    check('手机竖屏：点选下拉即时生效（微软雅黑栈落到正文）',
      snap.family.includes('YaHei'), JSON.stringify({ family: snap.family }));
    // 手工兜底在窄屏也要够宽可用
    await page.selectOption('#chatFontPick', 'custom');
    await page.waitForTimeout(300);
    const mobileCustom = await page.evaluate(() => {
      const input = document.querySelector('#chatFontFamilyCustom');
      const manualRow = document.querySelector('#chatFontManualRow');
      input.scrollIntoView({ block: 'center' });
      const rect = input.getBoundingClientRect();
      return {
        width: Math.round(rect.width),
        inViewport: rect.top >= 0 && rect.bottom <= window.innerHeight,
        manualVisible: manualRow ? !manualRow.hidden : null,
      };
    });
    check('手机竖屏：自定义输入框可用（够宽且在视口内）',
      mobileCustom.width > 150 && mobileCustom.inViewport === true && mobileCustom.manualVisible === true,
      JSON.stringify(mobileCustom));
    await page.screenshot({ path: 'verify/chat_font_smoke_3_mobile.png' });
    // 手机竖屏点选预设并保存：最后一道「窄屏也能落库」的证明
    await page.selectOption('#chatFontPick', 'lxgw');
    await page.waitForTimeout(250);
    posts.length = 0;
    await page.click('#saveAppearance');
    await page.waitForTimeout(900);
    const mobileSavedBody = posts.length === 1 ? JSON.parse(posts[0]).appearance : null;
    const mobileRemote = await readServerAppearance(page);
    check('手机竖屏保存可用（POST 带 lxgw，后端回读一致）',
      Boolean(mobileSavedBody) && mobileSavedBody.chat_font_family === 'lxgw'
      && mobileSavedBody.chat_font_size === 13 && mobileRemote.chat_font_family === 'lxgw',
      JSON.stringify({ count: posts.length, saved: mobileSavedBody, remote: mobileRemote.chat_font_family }));

    const pageErrors = errors.filter((item) => !item.includes('Failed to load resource'));
    const settingsErrors = badResponses.filter((item) => item.includes('/api/settings'));
    check('整轮设置写入都干净（无 __pick__ 占位、无 undefined 漏进 JSON）',
      allPosts.length >= 3
      && allPosts.every((body) => !body.includes('__pick__') && !body.includes('undefined')),
      JSON.stringify({ count: allPosts.length, sample: allPosts.slice(-2) }));
    check('零 pageerror / 设置接口零 4xx',
      pageErrors.length === 0 && settingsErrors.length === 0,
      JSON.stringify({ pageErrors: pageErrors.slice(0, 3), settingsErrors, badResponses: badResponses.slice(0, 5) }));
  } catch (error) {
    const message = String(error && error.message ? error.message : error).replace(/\s+/g, ' ');
    check('检查脚本未抛异常', false, message.slice(0, 700));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`会话字体冒烟：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exitCode = failures.length ? 1 : 0;
})();
