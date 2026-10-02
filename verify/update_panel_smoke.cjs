// 更新面板 + 输入框展开/折叠 的真后端冒烟（2026-10-02 三项改造的留证）。
//
// 与静态 public 服务的冒烟不同：本脚本要打 /api/update 与 /api/settings，必须连**真后端**。
// 用法：
//   1) NAIBA_TMP_PORT=8799 NAIBA_TMP_ROOT=verify/_tmp_upd_probe .venv\Scripts\python.exe verify\_serve_tmp.py
//   2) $env:NODE_PATH="D:\naiba-chat\node_modules"; node verify\update_panel_smoke.cjs
//
// 断言前先钉「缺陷前提成立」：
//   · `#updateReady` 是 display:flex 的元素 —— 若没有 [hidden] 压回规则，页面一打开横幅就会
//     露出来（做 HTML 预览时真踩过），所以第一条就是「初始必须不可见」。
//   · 展开态断言量的是**真实高度**（折叠 180px 封顶 vs 展开更高），不是只看类名。
const { chromium } = require('playwright');
const path = require('path');

const PORT = Number(process.env.NAIBA_UPD_SMOKE_PORT || 8799);
const BASE = `http://127.0.0.1:${PORT}`;
const ROOT = path.resolve(__dirname, '..');
const SHOT_DIR = path.join(ROOT, 'verify');

const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function apiSettings() {
  const response = await fetch(`${BASE}/api/bootstrap`);
  const payload = await response.json();
  return payload.settings || {};
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const consoleErrors = [];
  page.on('pageerror', (error) => consoleErrors.push(String(error)));

  await page.goto(`${BASE}/index.html`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#messageInput', { timeout: 15000 });
  // 首启引导弹窗会拦截点击（隔离实例每次都是「全新安装」），先按已有冒烟的做法移除它。
  await page.evaluate(() => document.getElementById('onboardingDialog')?.remove());
  await sleep(800);

  // ---- ① [hidden] 契约：三个控件初始都必须不可见（作者样式的 display 会顶掉 UA 的 hidden）----
  for (const id of ['updateReady', 'updateProgress', 'updateFloat']) {
    const visible = await page.evaluate((elementId) => {
      const el = document.getElementById(elementId);
      return Boolean(el) && getComputedStyle(el).display !== 'none';
    }, id);
    check(`① #${id} 初始不可见`, !visible, visible ? '被 display 规则顶掉了 hidden' : '');
  }

  // ---- ② 输入框展开/折叠：真实高度 + 类名 + 发送键旁位置 ----
  const collapsed = await page.evaluate(() => document.getElementById('messageInput').getBoundingClientRect().height);
  await page.click('#expandComposer');
  await sleep(250);
  const expandedState = await page.evaluate(() => {
    const wrap = document.querySelector('.composer-wrap:not(.edit-placeholder)');
    const button = document.getElementById('expandComposer');
    return {
      cls: wrap.classList.contains('is-expanded'),
      pressed: button.getAttribute('aria-pressed'),
      title: button.title,
      height: document.getElementById('messageInput').getBoundingClientRect().height,
    };
  });
  check('② 展开：.composer-wrap 获得 is-expanded', expandedState.cls);
  check('② 展开：按钮 aria-pressed=true 且标题改为「收起输入框」',
    expandedState.pressed === 'true' && expandedState.title.includes('收起'),
    `${expandedState.pressed} / ${expandedState.title}`);
  check('② 展开：输入框上限放开（可长得比折叠态更高）',
    expandedState.height >= collapsed, `${collapsed} -> ${expandedState.height}`);

  // 展开态在长文本下必须真的长高（钉住「上限确实生效」，而不是只是加了个类）
  await page.fill('#messageInput', Array.from({ length: 20 }, (_, i) => `第 ${i + 1} 行内容`).join('\n'));
  await page.dispatchEvent('#messageInput', 'input');
  await sleep(250);
  const longExpanded = await page.evaluate(() => document.getElementById('messageInput').getBoundingClientRect().height);
  check('② 展开 + 长文本：高度超过折叠封顶 180px', longExpanded > 180, `实测 ${longExpanded}px`);
  await page.screenshot({ path: path.join(SHOT_DIR, 'update_panel_expanded.png') });

  await page.click('#expandComposer');
  await sleep(250);
  const afterCollapse = await page.evaluate(() => ({
    cls: document.querySelector('.composer-wrap:not(.edit-placeholder)').classList.contains('is-expanded'),
    pressed: document.getElementById('expandComposer').getAttribute('aria-pressed'),
    height: document.getElementById('messageInput').getBoundingClientRect().height,
  }));
  check('② 收起：类移除、aria 复位、高度回落到 180px 封顶',
    !afterCollapse.cls && afterCollapse.pressed === 'false' && afterCollapse.height <= 180,
    JSON.stringify(afterCollapse));

  // 清空草稿，避免影响后续截图
  await page.fill('#messageInput', '');
  await page.dispatchEvent('#messageInput', 'input');

  // ---- ③ 软件更新页：代理开关四态 + 落库 + 「本次更新走」联动 ----
  await page.click('#openSettings');
  await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
  await page.click('[data-settings-tab="updates"]');
  await sleep(600);

  const proxyNow = await page.evaluate(() => ({
    checked: [...document.querySelectorAll('input[name="updateProxyMode"]')].find((el) => el.checked)?.value || '',
    state: document.getElementById('updateProxyState')?.textContent || '',
    urlRowHidden: document.getElementById('updateProxyUrlRow')?.hidden,
  }));
  const settings = await apiSettings();
  check('③ 代理开关反映服务端设置', proxyNow.checked === (settings.update_proxy || {}).mode,
    `页面 ${proxyNow.checked} vs 服务端 ${(settings.update_proxy || {}).mode}`);
  check('③ 手动模式才展开地址输入框',
    proxyNow.checked === 'manual' ? proxyNow.urlRowHidden === false : proxyNow.urlRowHidden === true,
    JSON.stringify(proxyNow));
  check('③ 「本次更新走」有生效说明', proxyNow.state.includes('本次更新走'), proxyNow.state);

  // 四态必须是「等宽分段控件」。缺陷前提：旧写法 label 79×64、文字块只有 13px 宽（竖排），
  // radio 被 `.settings-dialog label input{height:var(--set-ctl-h)}` 撑成 39×38 巨圆 —— 只看
  // 「有没有文字」测不出来，必须量几何。
  const proxyGeom = await page.evaluate(() => [...document.querySelectorAll('.update-proxy-option')].map((label) => {
    const cell = label.getBoundingClientRect();
    const radio = label.querySelector('input');
    const radioBox = radio.getBoundingClientRect();
    const spanBox = label.querySelector('span').getBoundingClientRect();
    return {
      w: Math.round(cell.width),
      h: Math.round(cell.height),
      top: Math.round(cell.top),
      radioH: Math.round(radioBox.height),
      opacity: getComputedStyle(radio).opacity,
      spanH: Math.round(spanBox.height),
    };
  }));
  check('③ 四个选项同排等宽（宽度差 ≤1px 且 top 一致）',
    proxyGeom.length === 4 && proxyGeom.every((g) => Math.abs(g.w - proxyGeom[0].w) <= 1 && Math.abs(g.top - proxyGeom[0].top) <= 1),
    JSON.stringify(proxyGeom));
  check('③ 选项文字横排单行（文字块高 ≤20px，不是竖排）',
    proxyGeom.every((g) => g.spanH <= 20), JSON.stringify(proxyGeom));
  check('③ 原生 radio 已视觉隐藏且不撑高（opacity=0、高 ≤44px、胶囊高 ≤44px）',
    proxyGeom.every((g) => g.opacity === '0' && g.radioH <= 44 && g.h <= 44), JSON.stringify(proxyGeom));

  await page.click('#updateProxyDirect');
  await sleep(900);
  const afterSwitch = await apiSettings();
  check('③ 切「强制直连」后落到服务端', (afterSwitch.update_proxy || {}).mode === 'direct',
    JSON.stringify(afterSwitch.update_proxy));
  const stateAfter = await page.textContent('#updateProxyState');
  check('③ 「本次更新走」随切换更新为直连', stateAfter.includes('直连'), stateAfter);

  await page.click('#updateProxyManual');
  await page.fill('#updateProxyUrl', 'http://127.0.0.1:7897');
  await page.dispatchEvent('#updateProxyUrl', 'change');
  await sleep(900);
  const manualSaved = await apiSettings();
  check('③ 手动地址保存成功', (manualSaved.update_proxy || {}).url === 'http://127.0.0.1:7897',
    JSON.stringify(manualSaved.update_proxy));

  // ---- ④ 悬浮卡：设置窗口打开时不得出现；关窗后无进行中的更新也不得出现 ----
  const floatWhileOpen = await page.evaluate(() => {
    const card = document.getElementById('updateFloat');
    return getComputedStyle(card).display !== 'none';
  });
  check('④ 设置窗口打开时悬浮卡不出现', !floatWhileOpen);

  await page.screenshot({ path: path.join(SHOT_DIR, 'update_panel_settings.png') });

  await page.click('#settingsDialog [data-close="settingsDialog"]');
  await sleep(600);
  const floatAfterClose = await page.evaluate(() => {
    const card = document.getElementById('updateFloat');
    return getComputedStyle(card).display !== 'none';
  });
  check('④ 无进行中的更新时，关窗后悬浮卡也不出现（负向断言）', !floatAfterClose);

  // ---- ⑤ 恢复默认「跟随全局」，别把探针实例的配置留成手动代理 ----
  await page.click('#openSettings');
  await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
  await page.click('[data-settings-tab="updates"]');
  await sleep(400);
  await page.click('#updateProxyInherit');
  await sleep(900);
  const restored = await apiSettings();
  check('⑤ 已恢复默认「跟随全局」', (restored.update_proxy || {}).mode === 'inherit',
    JSON.stringify(restored.update_proxy));
  // 关掉设置窗口，后面的 ⑦ 段要重新从主界面点开设置（对话框是模态的，不关会拦截点击）。
  await page.click('#settingsDialog [data-close="settingsDialog"]');
  await sleep(500);

  check('⑥ 页面无 JS 异常', consoleErrors.length === 0, consoleErrors.join(' | '));

  // ---- ⑦ 下载中 / 卡死 / 待重启 的 UI 路径 ----
  // 用路由伪造 /api/update 的返回，把「下载中」和「待重启」两个态推进前端（不需要真发一次版）。
  const openUpdatesTab = async () => {
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('[data-settings-tab="updates"]');
    await sleep(700);
  };
  const closeSettings = async () => {
    await page.click('#settingsDialog [data-close="settingsDialog"]');
    await sleep(600);
  };
  const visible = (id) => page.evaluate((elementId) => {
    const el = document.getElementById(elementId);
    return Boolean(el) && getComputedStyle(el).display !== 'none';
  }, id);

  const realStatus = await (await fetch(`${BASE}/api/update`)).json();
  const zeroDownload = { received: 0, total: 0, speed: 0, percent: 0, stalled: false };
  let fakeStatus = {
    ...realStatus,
    phase: 'downloading',
    can_cancel: true,
    ready: {},
    can_apply: false,
    download: { received: 40 * 1024 * 1024, total: 96 * 1024 * 1024, speed: 1.2 * 1024 * 1024, percent: 42, stalled: false },
  };
  await page.route('**/api/update', (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(fakeStatus) }));
  await page.route('**/api/update/cancel', (route) => {
    fakeStatus = { ...fakeStatus, phase: 'available', can_cancel: false, download: zeroDownload };
    route.fulfill({ contentType: 'application/json', body: JSON.stringify(fakeStatus) });
  });
  await page.route('**/api/update/discard', (route) => {
    fakeStatus = { ...fakeStatus, phase: 'available', ready: {}, can_apply: false, download: zeroDownload };
    route.fulfill({ contentType: 'application/json', body: JSON.stringify(fakeStatus) });
  });

  await openUpdatesTab();
  check('⑦ 下载中：进度块可见', await visible('updateProgress'));
  const progress = await page.evaluate(() => {
    const track = document.querySelector('#updateProgress .update-progress-track');
    const fill = document.getElementById('updateProgressFill');
    const trackBox = track.getBoundingClientRect();
    const fillBox = fill.getBoundingClientRect();
    return {
      width: fill.style.width,
      text: document.getElementById('updateProgressText').textContent,
      speed: document.getElementById('updateProgressSpeed').textContent,
      trackW: Math.round(trackBox.width),
      trackH: Math.round(trackBox.height),
      fillW: Math.round(fillBox.width),
    };
  });
  check('⑦ 下载中：进度百分比与文案正确',
    progress.width === '42%' && progress.text.includes('/') && progress.text.includes('MB'),
    JSON.stringify(progress));
  // 几何断言：只看 visibility 会漏掉「元素在但宽高为 0」的假绿（.update-status > div 的 flex 曾把它压塌）
  check('⑦ 下载中：轨道有真实宽高、填充占比与百分比一致',
    progress.trackW > 200 && progress.trackH >= 6 && Math.abs(progress.fillW / progress.trackW - 0.42) < 0.03,
    JSON.stringify(progress));
  check('⑦ 下载中：显示「取消下载」并隐藏「立即更新」',
    (await visible('cancelUpdate')) && !(await visible('installUpdate')));

  await closeSettings();
  check('⑦ 关窗后：悬浮卡接管显示', await visible('updateFloat'));
  const floatMeta = await page.textContent('#updateFloatMeta');
  check('⑦ 悬浮卡显示进度', floatMeta.includes('42%'), floatMeta);

  await openUpdatesTab();
  fakeStatus = { ...fakeStatus, download: { ...fakeStatus.download, stalled: true, speed: 0 } };
  await closeSettings();
  await openUpdatesTab();
  const stalled = await page.evaluate(() => ({
    cls: document.getElementById('updateProgressFill').classList.contains('stalled'),
    speed: document.getElementById('updateProgressSpeed').textContent,
  }));
  check('⑦ 卡死：进度条标黄并给出排查提示',
    stalled.cls && stalled.speed.includes('卡死'), JSON.stringify(stalled));
  await page.screenshot({ path: path.join(SHOT_DIR, 'update_panel_downloading.png') });

  await closeSettings();
  await page.click('#updateFloatCancel');
  await sleep(900);
  check('⑦ 取消后：悬浮卡与进度块都收起',
    !(await visible('updateFloat')) && !(await visible('updateProgress')));

  // 待重启态：重启时机交给用户（默认先给「立即重启 / 稍后重启」，稍后重启后转常驻横幅）
  fakeStatus = {
    ...realStatus,
    phase: 'ready',
    can_cancel: false,
    download: zeroDownload,
    ready: { version: '9.9.9-beta', commit: 'f'.repeat(40), downloaded_at: Math.floor(Date.now() / 1000) },
    can_apply: true,
  };
  await openUpdatesTab();
  check('⑦ 已下载：出现「立即重启 / 稍后重启」两个选择',
    (await visible('restartNow')) && (await visible('restartLater')));
  check('⑦ 已下载：不再显示「立即更新」', !(await visible('installUpdate')));
  await page.click('#restartLater');
  await sleep(300);
  check('⑦ 稍后重启：转为常驻待重启横幅', await visible('updateReady'));
  await closeSettings();
  check('⑦ 稍后重启后关窗：悬浮卡转为「更新待重启」并给立即重启入口',
    (await visible('updateFloat')) && (await visible('updateFloatApply')));
  await page.screenshot({ path: path.join(SHOT_DIR, 'update_panel_float_ready.png') });

  await page.click('#updateFloatDiscard');
  await sleep(900);
  check('⑦ 放弃更新：悬浮卡收起', !(await visible('updateFloat')));

  await page.unroute('**/api/update');
  await page.unroute('**/api/update/cancel');
  await page.unroute('**/api/update/discard');

  await browser.close();
  console.log(failures.length ? `\nFAILED (${failures.length}): ${failures.join(', ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('SMOKE CRASH:', error);
  process.exit(2);
});
