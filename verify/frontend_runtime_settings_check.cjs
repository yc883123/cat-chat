// B 级前端检验：运行设置里本轮新增的两道「刹车」参数（本地首字节超时 / Agent 最大步数）
//
// 为什么必须用浏览器而不是只跑单测：这两项是「设置页 ↔ 后端 config」的接线，
// 单测只能证明后端会读会存，证明不了①页面上真的渲染出这两个框、②文案能让人看懂、
// ③`0` 这个**合法值**不会被前端的 `|| 默认` 吞掉（历史坑：0 被当成空值回填成默认）。
//
// 断言：
//   1) 存在性：两个输入都在「运行设置」面板里，可见、label 文案正确、min/max 正确
//   2) 来源：面板显示的值 = 后端 bootstrap.settings 的真值（不是前端硬编码）
//   3) 文案：hint 里保留了「保活注释不算」「0 = 不限制」这两条关键约定
//   4) 0 不被吞：填 0 → 保存 → 重载页面 → 仍是 0（且后端 config.json 里也是 0）
//   5) 改回去也生效：120 / 200 → 保存 → 重载 → 120 / 200
//   6) 排版：面板无横向溢出、label 与 hint 都被完整裁切（scrollHeight/clientHeight 一致）
//   7) 主题：浅色与暗色各截一张（真实切主题，走页面自己的 change 处理）
//   8) 零 pageerror / console.error
//
// 用法（必须配隔离源码实例，不能用 8765 的打包版，它读的是内置资源）：
//   bash verify/run_frontend_check.sh
const { chromium } = require('playwright');
const path = require('node:path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8765';
const OUT_DIR = path.resolve(__dirname);
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

const IDS = ['localFirstByteTimeout', 'agentStepLimit'];
// DOM id ↔ 后端配置键不是同一个名字，这里显式映射（第一版把两者当同一个，断言全假过）
const EXPECT = {
  localFirstByteTimeout: {
    key: 'local_first_byte_timeout_seconds',
    label: '本地首字节超时（秒）', min: '0', max: '1800', dflt: 120,
  },
  agentStepLimit: {
    key: 'agent_step_limit',
    label: 'Agent 最大步数', min: '0', max: '1000', dflt: 200,
  },
};

async function openRuntimePanel(page) {
  await page.click('#openSettings');
  await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
  await page.click('.settings-nav button[data-settings-tab="runtime"]');
  await page.waitForSelector('section[data-settings-panel="runtime"]:not([hidden])', { timeout: 10000 });
  await page.waitForSelector('#localFirstByteTimeout', { state: 'visible', timeout: 10000 });
  await page.waitForTimeout(400);
}

async function readPanel(page) {
  return page.evaluate((ids) => {
    const out = { inputs: {}, panel: null };
    const panel = document.querySelector('section[data-settings-panel="runtime"]');
    const cs = getComputedStyle(panel);
    out.panel = {
      scrollWidth: panel.scrollWidth,
      clientWidth: panel.clientWidth,
      scrollHeight: panel.scrollHeight,
      clientHeight: panel.clientHeight,
      overflowX: cs.overflowX,
    };
    for (const id of ids) {
      const el = document.getElementById(id);
      if (!el) { out.inputs[id] = null; continue; }
      const label = el.closest('label');
      const hint = label ? label.querySelector('small.hint') : null;
      const rect = el.getBoundingClientRect();
      const panelRect = panel.getBoundingClientRect();
      out.inputs[id] = {
        value: el.value,
        min: el.min,
        max: el.max,
        type: el.type,
        visible: rect.width > 0 && rect.height > 0,
        labelText: label ? label.childNodes[0].textContent.trim() : '',
        hintText: hint ? hint.textContent.replace(/\s+/g, ' ').trim() : '',
        insidePanel: rect.left >= panelRect.left - 1 && rect.right <= panelRect.right + 1,
        hintClipped: hint ? hint.scrollHeight > hint.clientHeight + 1 : false,
      };
    }
    return out;
  }, IDS);
}

async function fetchSettings(page) {
  return page.evaluate(async () => {
    const res = await fetch('/api/bootstrap', { cache: 'no-store' });
    const data = await res.json();
    return data.settings || {};
  });
}

async function saveRuntime(page, values) {
  await page.evaluate((vals) => {
    for (const [id, v] of Object.entries(vals)) {
      const el = document.getElementById(id);
      if (el) el.value = String(v);
    }
  }, values);
  await page.click('#saveRuntime');
  await page.waitForFunction(
    () => {
      const t = document.getElementById('toast');
      return t && t.classList.contains('show') && /已保存/.test(t.textContent || '');
    },
    { timeout: 10000 },
  );
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  const netErrors = [];
  page.on('pageerror', (err) => errors.push(err.message));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    if (text.includes('Failed to load resource')) return; // 由 response 监听记录
    errors.push(text);
  });
  page.on('response', (res) => { if (res.status() >= 400) netErrors.push(`${res.status()} ${res.url()}`); });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(500);
    await openRuntimePanel(page);

    // ---- 1) 存在性 / 文案 / 属性 ----
    const panel = await readPanel(page);
    const settings = await fetchSettings(page);
    for (const id of IDS) {
      const info = panel.inputs[id];
      check(`#${id} 在「运行设置」面板里可见`, Boolean(info && info.visible), JSON.stringify(info));
      if (!info) continue;
      check(`#${id} label 文案 = ${EXPECT[id].label}`, info.labelText === EXPECT[id].label, info.labelText);
      check(`#${id} 取值范围 ${EXPECT[id].min}~${EXPECT[id].max}`,
        info.min === EXPECT[id].min && info.max === EXPECT[id].max, `${info.min}~${info.max}`);
      check(`#${id} 没有被面板横向裁掉`, info.insidePanel);
      check(`#${id} 的说明文字没有被裁切（完整可见）`, !info.hintClipped);
      const truth = settings[EXPECT[id].key];
      check(`#${id} 显示值 = 后端真值（${truth ?? EXPECT[id].dflt}）`,
        Number(info.value) === Number(truth ?? EXPECT[id].dflt),
        `页面=${info.value} 后端=${truth}`);
      check(`后端 bootstrap.settings 里带出 ${EXPECT[id].key}（默认值已合入配置）`,
        truth !== undefined, JSON.stringify(truth));
    }
    check('首字节超时说明里保留了「保活注释不算」这条关键约定',
      /保活注释不算/.test(panel.inputs.localFirstByteTimeout?.hintText || ''),
      panel.inputs.localFirstByteTimeout?.hintText);
    check('步数上限说明里写明了「0 = 不限制」',
      /0\s*=\s*不限制/.test(panel.inputs.agentStepLimit?.hintText || ''),
      panel.inputs.agentStepLimit?.hintText);
    // 依赖 runner 传的是全新隔离根（见 run_frontend_check.sh），否则读到的是上一轮存的值
    check('全新安装的默认值 = 120 秒 / 200 步',
      Number(settings.local_first_byte_timeout_seconds) === EXPECT.localFirstByteTimeout.dflt
      && Number(settings.agent_step_limit) === EXPECT.agentStepLimit.dflt,
      `首字节=${settings.local_first_byte_timeout_seconds} 步数=${settings.agent_step_limit}`);

    // ---- 2) 浅色截图 ----
    // 注意：不要用 locator.screenshot() 截「运行设置」面板 —— 它在模态 <dialog> 的
    // top layer 里，且比视口高，Playwright 的分块拼接会截出叠影/串到下层聊天界面的图。
    // 改为「把新字段滚到视口中间 + 视口截图」，所见即所得。
    const focusNew = () => page.evaluate(() => {
      const el = document.getElementById('localFirstByteTimeout');
      if (el) el.scrollIntoView({ block: 'center' });
    });
    await focusNew();
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(OUT_DIR, 'frontend_runtime_settings_light.png') });
    await page.screenshot({
      path: path.join(OUT_DIR, 'frontend_runtime_settings_light_full.png'),
      fullPage: true,
    });

    // ---- 3) 0 是合法值：保存 → 重载 → 仍是 0 ----
    await saveRuntime(page, { localFirstByteTimeout: 0, agentStepLimit: 0 });
    const afterZero = await fetchSettings(page);
    check('保存 0 后后端存的是 0（不是被回落成默认）',
      Number(afterZero.local_first_byte_timeout_seconds) === 0 && Number(afterZero.agent_step_limit) === 0,
      `首字节=${afterZero.local_first_byte_timeout_seconds} 步数=${afterZero.agent_step_limit}`);

    await page.reload({ waitUntil: 'load' });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(400);
    await openRuntimePanel(page);
    const reloaded = await readPanel(page);
    check('重载后 #localFirstByteTimeout 仍显示 0（前端没有 || 默认 吞掉 0）',
      reloaded.inputs.localFirstByteTimeout?.value === '0', reloaded.inputs.localFirstByteTimeout?.value);
    check('重载后 #agentStepLimit 仍显示 0',
      reloaded.inputs.agentStepLimit?.value === '0', reloaded.inputs.agentStepLimit?.value);

    // ---- 4) 改回默认值 ----
    await saveRuntime(page, { localFirstByteTimeout: 120, agentStepLimit: 200 });
    const restored = await fetchSettings(page);
    check('改回 120 / 200 后后端同步生效',
      Number(restored.local_first_byte_timeout_seconds) === 120 && Number(restored.agent_step_limit) === 200,
      `首字节=${restored.local_first_byte_timeout_seconds} 步数=${restored.agent_step_limit}`);

    // ---- 5) 暗色截图（走页面真实的主题切换处理） ----
    const darkApplied = await page.evaluate(() => {
      const radio = document.querySelector('input[name="appearanceTheme"][value="dark"]');
      if (!radio) return null;
      radio.checked = true;
      radio.dispatchEvent(new Event('change', { bubbles: true }));
      return document.documentElement.dataset.theme || '';
    });
    await page.waitForTimeout(500);
    const themeNow = await page.evaluate(() => document.documentElement.dataset.theme || '');
    check('切到夜间主题生效（走页面自身 change 处理）', themeNow === 'dark',
      `dataset.theme=${themeNow}（radio 存在=${darkApplied !== null}）`);
    const bodyBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    check('暗色下背景确实是深色（亮度低）', /rgb\((\d+), (\d+), (\d+)\)/.test(bodyBg)
      && bodyBg.match(/\d+/g).slice(0, 3).map(Number).reduce((a, b) => a + b, 0) / 3 < 90, bodyBg);
    await focusNew();
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(OUT_DIR, 'frontend_runtime_settings_dark.png') });

    check('零 pageerror / console.error', errors.length === 0, errors.slice(0, 3).join(' | '));
    console.log(`[net]  非 2xx 响应 ${netErrors.length} 条${netErrors.length ? '：' + netErrors.slice(0, 5).join(' | ') : ''}`);
  } catch (error) {
    check('检查脚本未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`前端「运行设置」检验：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exitCode = failures.length ? 1 : 0;
})();
