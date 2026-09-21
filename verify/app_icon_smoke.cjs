// 「应用内更换图标」的浏览器侧检查（由 verify/app_icon_smoke.py 自编排：它先起隔离源码实例
// （独立 config / data_dir / 端口），把地址经 NAIBA_SMOKE_BASE 传进来，并给出待上传图片路径）。
//
// 全部走真后端真接口：弹窗打开 → 真上传 → 真落盘 → 预览图是**服务端归一化后的方形图**
// （用 naturalWidth 证明，不是前端自己画的）→ 关掉重开仍在（落盘持久）→ 恢复默认回内置。
//
// 环境变量：NAIBA_SMOKE_BASE（必填）、NAIBA_ICON_FIXTURE（待上传 PNG 的绝对路径，必填）
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || '';
const FIXTURE = process.env.NAIBA_ICON_FIXTURE || '';
// 夹具是 300×600 的非方形图：服务端归一化后长边 600 ⇒ 预览图的 naturalWidth 必须是 600。
// 这条断言同时证明"真落盘了"和"留白贴方形真的生效"。
const EXPECTED_SQUARE = 600;

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function dialogState(page) {
  return page.evaluate(() => {
    const dialog = document.querySelector('#appIconDialog');
    const preview = document.querySelector('#appIconPreview');
    const reset = document.querySelector('#appIconReset');
    const entry = document.querySelector('#openAppIcon');
    const settings = document.querySelector('#openSettings');
    return {
      open: Boolean(dialog && dialog.open),
      status: (document.querySelector('#appIconStatus') || {}).textContent || '',
      error: (document.querySelector('#appIconError') || {}).hidden ? '' : (document.querySelector('#appIconError') || {}).textContent || '',
      resetHidden: Boolean(reset && reset.hidden),
      previewLoaded: Boolean(preview && preview.complete && preview.naturalWidth > 0),
      previewWidth: preview ? preview.naturalWidth : 0,
      entryAfterSettings: Boolean(entry && settings && (settings.compareDocumentPosition(entry) & Node.DOCUMENT_POSITION_FOLLOWING)),
      entryVisible: Boolean(entry && entry.getClientRects().length),
    };
  });
}

(async () => {
  if (!BASE || !FIXTURE) {
    console.log('FAIL  缺少 NAIBA_SMOKE_BASE / NAIBA_ICON_FIXTURE（请通过 verify/app_icon_smoke.py 运行）');
    process.exit(1);
  }
  let code = 1;
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    // 隔离实例的两张占位供应商都没有 api_key ⇒ 首启引导弹层会自己弹出来（top layer 拦点击）。
    // 图标功能与它无关，进页面前置为"已跳过"。
    await context.addInitScript(() => {
      try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) { /* 无存储权限时忽略 */ }
    });
    const page = await context.newPage();
    const pageErrors = [];
    let failedResourceConsole = 0;
    page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
    page.on('console', (msg) => {
      if (msg.type() !== 'error') return;
      const text = msg.text();
      // 「Failed to load resource」不带 URL；它对应哪一条由 badResponses 逐条带 URL 判。
      if (text.includes('Failed to load resource')) { failedResourceConsole += 1; return; }
      pageErrors.push(`console.error: ${text}`);
    });
    // 已知取样噪音：隔离实例的供应商是 `https://example.invalid/v1`（_serve_tmp.py 的占位），
    // 模型目录必然连不通 → 后端回 400。**并计数**：排除条数必须与"socket 层报错条数"相等，
    // 免得白名单顺手把真报错一起吞掉。
    const KNOWN_NOISE = /\/api\/providers\/models$/;
    let noiseCount = 0;
    const badResponses = [];
    page.on('response', (res) => {
      if (res.status() < 400) return;
      if (KNOWN_NOISE.test(res.url())) { noiseCount += 1; return; }
      badResponses.push(`${res.status()} ${res.url()}`);
    });

    await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
    await page.waitForSelector('#openAppIcon', { timeout: 30000 });
    await page.waitForTimeout(600);

    // ① 入口形态：在齿轮右侧、可见
    let state = await dialogState(page);
    check('侧栏底部有「更换应用图标」入口，且在设置齿轮右侧', state.entryAfterSettings && state.entryVisible);
    check('弹窗初始关闭', !state.open);

    // ② 打开弹窗：默认态 = 内置图标预览可加载、「恢复默认」隐藏
    await page.click('#openAppIcon');
    await page.waitForSelector('#appIconDialog[open]', { timeout: 10000 });
    await page.waitForFunction(() => {
      const img = document.querySelector('#appIconPreview');
      return img && img.complete && img.naturalWidth > 0;
    }, undefined, { timeout: 15000 });
    state = await dialogState(page);
    const builtinWidth = state.previewWidth;
    check('弹窗打开', state.open);
    check('默认态文案为「正在使用默认图标」', state.status.includes('默认'), state.status);
    check('默认态隐藏「恢复默认」', state.resetHidden);
    check('预览图真的加载出内置图标', state.previewLoaded && builtinWidth > 0, `naturalWidth=${builtinWidth}`);
    check('默认态预览图不是自定义图', builtinWidth !== EXPECTED_SQUARE, `naturalWidth=${builtinWidth}`);
    await page.screenshot({ path: 'verify/app_icon_smoke_1_default.png' });

    // ③ 真上传（走 #appIconFile 的 change，与用户点「选择图片」同一条路径）
    await page.setInputFiles('#appIconFile', FIXTURE);
    await page.waitForFunction(
      () => (document.querySelector('#appIconStatus') || {}).textContent.includes('自定义'),
      undefined, { timeout: 20000 },
    );
    await page.waitForFunction((expected) => {
      const img = document.querySelector('#appIconPreview');
      return img && img.complete && img.naturalWidth === expected;
    }, EXPECTED_SQUARE, { timeout: 20000 });
    state = await dialogState(page);
    check('上传后状态转「正在使用自定义图标」', state.status.includes('自定义'), state.status);
    check('上传后出现「恢复默认」', !state.resetHidden);
    check(
      `预览图 = 服务端归一化后的方形图（${EXPECTED_SQUARE}px）`,
      state.previewWidth === EXPECTED_SQUARE,
      `naturalWidth=${state.previewWidth}`,
    );
    check('上传成功无错误提示', state.error === '', state.error);
    const toastText = await page.evaluate(() => (document.querySelector('#toast') || {}).textContent || '');
    check('toast 提示「完全退出并重新启动」', toastText.includes('重新启动'), toastText);
    await page.screenshot({ path: 'verify/app_icon_smoke_2_custom.png' });

    // ④ 关掉重开：状态是服务端来的（落盘持久），不是前端内存
    await page.click('#appIconDialog [data-close="appIconDialog"]');
    await page.waitForFunction(() => !document.querySelector('#appIconDialog').open, undefined, { timeout: 10000 });
    await page.click('#openAppIcon');
    await page.waitForFunction(() => document.querySelector('#appIconDialog').open, undefined, { timeout: 10000 });
    await page.waitForFunction((expected) => {
      const img = document.querySelector('#appIconPreview');
      return img && img.complete && img.naturalWidth === expected;
    }, EXPECTED_SQUARE, { timeout: 20000 });
    state = await dialogState(page);
    check('关掉重开仍是自定义态（真的落盘了）', state.status.includes('自定义') && !state.resetHidden, state.status);

    // ⑤ 恢复默认：回内置图标
    await page.click('#appIconReset');
    await page.waitForFunction(
      () => (document.querySelector('#appIconStatus') || {}).textContent.includes('默认'),
      undefined, { timeout: 20000 },
    );
    await page.waitForFunction((expected) => {
      const img = document.querySelector('#appIconPreview');
      return img && img.complete && img.naturalWidth === expected;
    }, builtinWidth, { timeout: 20000 });
    state = await dialogState(page);
    check('恢复默认后状态回内置', state.status.includes('默认'), state.status);
    check('恢复默认后重新隐藏「恢复默认」', state.resetHidden);
    check('恢复默认后预览图回内置图', state.previewWidth === builtinWidth, `naturalWidth=${state.previewWidth}`);
    const resetToast = await page.evaluate(() => (document.querySelector('#toast') || {}).textContent || '');
    check('恢复默认也有重启提示', resetToast.includes('重新启动'), resetToast);

    // ⑥ 收尾：零 JS 错误 / 零计划外失败请求 / 白名单并计数
    check('零页面错误', pageErrors.length === 0, pageErrors.join(' | '));
    check('零计划外 4xx/5xx', badResponses.length === 0, badResponses.join(' | '));
    check(
      '白名单排除条数与 socket 层报错条数一致（白名单没吞真报错）',
      noiseCount === failedResourceConsole,
      `noise=${noiseCount} console=${failedResourceConsole}`,
    );

    code = failures.length === 0 ? 0 : 1;
    console.log(`\n${code === 0 ? 'ALL PASS' : `FAILED: ${failures.length}`}`);
  } catch (error) {
    console.log(`FAIL  脚本异常：${error && error.message}`);
    code = 1;
  } finally {
    await browser.close();
  }
  process.exit(code);
})();
