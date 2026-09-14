// A 级针对性检查：暗色主题下「软色底」组件的文字对比度。
// 背景：html[data-theme="dark"] 覆盖了 --neutral-*/--text 等，但没覆盖换肤软色变量
//       （--accent-soft / --accent-soft-2 / --violet-*），它们在 :root 与各 skin 里都是近白色，
//       于是出现「近白底 + 近白字」：正文与卡片名几乎不可读（实测对比度 1.05）。
// 断言（暗色 + 浅色两套）：
//   1) .active-task-bar（Run 运行中提示条）正文/背景对比度 >= 4.5
//   2) .provider-card.is-default（API 卡片「当前」态）卡名/卡片背景对比度 >= 4.5
//   3) 暗色下软色底必须真的变暗（亮度 <= 0.5）
//   4) 浅色主题不回归
// 运行：node verify\_check_dark_soft_contrast.cjs（服务需在 NAIBA_SMOKE_BASE 上运行）
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8765';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

const parseRgb = (value) => (String(value).match(/[\d.]+/g) || []).slice(0, 3).map(Number);
const luminance = (rgb) => {
  const channel = rgb.map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * channel[0] + 0.7152 * channel[1] + 0.0722 * channel[2];
};
const contrast = (fg, bg) => {
  const a = luminance(parseRgb(fg));
  const b = luminance(parseRgb(bg));
  const [hi, lo] = a > b ? [a, b] : [b, a];
  return (hi + 0.05) / (lo + 0.05);
};

const PROBE_STYLE = 'position:fixed;right:14px;bottom:14px;width:420px;z-index:99999;';

async function sample(page, theme) {
  return page.evaluate((args) => {
    const { themeName, probeStyle } = args;
    document.documentElement.dataset.theme = themeName;

    // Run 运行中提示条：真实 #activeTaskBar 元素，文本与 renderRunTasks() 完全一致
    const bar = document.querySelector('#activeTaskBar');
    bar.hidden = false;
    bar.innerHTML = '当前对话有 1 个 Run 正在执行。<button type="button" data-open-tasks>查看</button>';

    // API 卡片「当前」态：与 providerCardMarkup() 同结构的真实类名注入（放右下角便于截图核对）
    let host = document.querySelector('#__soft_probe');
    if (!host) {
      host = document.createElement('div');
      host.id = '__soft_probe';
      host.className = 'provider-cards';
      host.style.cssText = probeStyle;
      document.body.appendChild(host);
    }
    host.innerHTML = `<div class="provider-card is-default" data-provider-card="probe" role="button" tabindex="0">
        <span class="provider-card-name" title="TE">TE</span>
        <span class="provider-card-foot">
          <span class="provider-card-tag">OpenAI 兼容</span>
          <span class="provider-card-badge">当前</span>
        </span>
      </div>
      <div class="provider-card" data-provider-card="probe2" role="button" tabindex="0">
        <span class="provider-card-name" title="魔搭社区">魔搭社区</span>
        <span class="provider-card-foot"><span class="provider-card-tag">OpenAI 兼容</span></span>
      </div>`;

    const style = (el) => {
      const s = getComputedStyle(el);
      return { color: s.color, background: s.backgroundColor };
    };
    const card = host.querySelector('.provider-card.is-default');
    const name = card.querySelector('.provider-card-name');
    const plain = host.querySelectorAll('.provider-card')[1];

    return {
      bar: style(bar),
      card: style(card),
      cardName: style(name),
      plainCard: style(plain),
      plainName: style(plain.querySelector('.provider-card-name')),
    };
  }, { themeName: theme, probeStyle: PROBE_STYLE });
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1180, height: 820 } });
  const errors = [];
  const netErrors = [];
  page.on('pageerror', (err) => errors.push(err.message));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    // 资源加载失败统一由 response 监听记录（带 URL），这里只留真正的脚本报错
    if (text.includes('Failed to load resource')) return;
    errors.push(text);
  });
  page.on('response', (res) => { if (res.status() >= 400) netErrors.push(`${res.status()} ${res.url()}`); });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(600);

    const dark = await sample(page, 'dark');
    const dBar = contrast(dark.bar.color, dark.bar.background);
    const dName = contrast(dark.cardName.color, dark.card.background);
    const dPlain = contrast(dark.plainName.color, dark.plainCard.background);
    console.log(`[dark]  提示条 正文 ${dark.bar.color} / 底 ${dark.bar.background} = ${dBar.toFixed(2)}`);
    console.log(`[dark]  当前卡 卡名 ${dark.cardName.color} / 底 ${dark.card.background} = ${dName.toFixed(2)}`);
    console.log(`[dark]  普通卡 卡名 ${dark.plainName.color} / 底 ${dark.plainCard.background} = ${dPlain.toFixed(2)}`);
    check('暗色：Run 运行中提示条文字可读（对比度 >= 4.5）', dBar >= 4.5, dBar.toFixed(2));
    check('暗色：API「当前」卡片名可读（对比度 >= 4.5）', dName >= 4.5, dName.toFixed(2));
    check('暗色：普通 API 卡片名可读（对比度 >= 4.5）', dPlain >= 4.5, dPlain.toFixed(2));
    check('暗色：软色底确实变暗（亮度 <= 0.5）', luminance(parseRgb(dark.card.background)) <= 0.5,
      dark.card.background);

    await page.evaluate(() => document.querySelector('#activeTaskBar').scrollIntoView({ block: 'center' }));
    await page.waitForTimeout(300);
    await page.screenshot({ path: 'verify/_dark_soft_contrast_injected.png' });

    // 真实路径：打开设置 → API 供应商，采样真实「当前」卡片（有就核对，没有不算失败）
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="models"]');
    await page.waitForSelector('#providerCards .provider-card', { timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(600);
    const real = await page.evaluate(() => {
      const card = document.querySelector('#providerCards .provider-card.is-default');
      if (!card) return null;
      const name = card.querySelector('.provider-card-name');
      return {
        text: name ? name.textContent : '',
        color: getComputedStyle(name).color,
        background: getComputedStyle(card).backgroundColor,
      };
    });
    if (real) {
      const ratio = contrast(real.color, real.background);
      console.log(`[dark]  真实设置面板「${real.text}」卡 ${real.color} / 底 ${real.background} = ${ratio.toFixed(2)}`);
      check('暗色：真实设置面板「当前」卡片名可读', ratio >= 4.5, ratio.toFixed(2));
    } else {
      console.log('[dark]  真实设置面板暂无「当前」卡片，跳过该断言（注入探针已覆盖同一 CSS）');
    }
    await page.screenshot({ path: 'verify/_dark_soft_contrast_real_panel.png' });

    // 关掉设置面板再做浅色回归
    await page.evaluate(() => document.querySelector('#settingsDialog').close());
    await page.waitForTimeout(300);
    const light = await sample(page, 'light');
    const lBar = contrast(light.bar.color, light.bar.background);
    const lName = contrast(light.cardName.color, light.card.background);
    console.log(`[light] 提示条 正文 ${light.bar.color} / 底 ${light.bar.background} = ${lBar.toFixed(2)}`);
    console.log(`[light] 当前卡 卡名 ${light.cardName.color} / 底 ${light.card.background} = ${lName.toFixed(2)}`);
    check('浅色：提示条与卡片名仍可读（对比度 >= 4.5）', lBar >= 4.5 && lName >= 4.5,
      `${lBar.toFixed(2)} / ${lName.toFixed(2)}`);

    check('零 pageerror / console.error', errors.length === 0, errors.slice(0, 3).join(' | '));
    // 隔离空库固有噪声（无会话/无工作区时某些接口返回 4xx）不算失败，但要打出来留痕
    console.log(`[net]  非 2xx 响应 ${netErrors.length} 条${netErrors.length ? '：' + netErrors.slice(0, 5).join(' | ') : ''}`);
  } catch (error) {
    check('检查脚本未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`暗色软色底对比度检查：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exitCode = failures.length ? 1 : 0;
})();
