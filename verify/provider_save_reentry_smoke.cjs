// 「保存设置」防重入 + 后端幂等冒烟（真后端，§九.127）。
//
// 事故形态（用户实测）：慢上游的模型目录探测要等好几秒到十几秒，这段等待里按钮不禁用、
// 一句提示都没有 ⇒ 用户以为"点了没反应"就继续点 ⇒ 连点 5 次 = 5 张同样的卡片。
// 本脚本钉住三件事：
//   ① 慢探测阶段：按钮进「保存中…」禁用态，且报出进度提示（消除"点了没反应"）；
//   ② 保存进行中再点/再回车 3 次，**只发出 1 次保存请求**（disabled 挡得住鼠标点击，
//      挡不住在输入框里按回车，所以必须靠重入闸）；
//   ③ 绕过 UI 直接连发 3 个无 id 的 POST，后端也只落一条（第二层兜底）；不同 Key 仍并存。
//
// 为什么必须真后端：卡片数量、幂等行为都由 /api/bootstrap 与 /api/providers 驱动，
// 只开静态 public/ 会让整块配置驱动的 UI 消失而断言照样全绿（§九.92 的坑）。
//
// 已知取样噪音：`/api/providers/models` 被本脚本主动路由成「延迟 900ms 后 400」——
// 既复现"慢上游"的等待窗口，又不去真连占位域名 example.invalid。按 §六 惯例排除。
//
// 前置：源码 server 已在 NAIBA_SMOKE_BASE（默认 8799）运行（由 provider_save_reentry_smoke.py 起）。
// 运行：node verify\provider_save_reentry_smoke.cjs
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const SHOTS = process.env.NAIBA_SMOKE_SHOTS || '.';
const PREFIX = '连点供应商';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

const KNOWN_NOISE = /\/api\/providers\/models/;

async function apiJson(pathname, options = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(`${BASE}${pathname}`, init);
  return response.json().catch(() => ({}));
}

function seeded(name) {
  return String(name || '').startsWith(PREFIX);
}

async function cleanupSeeded() {
  const bootstrap = await apiJson('/api/bootstrap');
  for (const provider of bootstrap.model_profiles || bootstrap.providers || []) {
    if (!seeded(provider.name)) continue;
    await apiJson(`/api/providers/${encodeURIComponent(provider.id)}`, { method: 'DELETE' });
  }
}

function names(profiles) {
  return profiles.filter((item) => seeded(item.name)).map((item) => item.name);
}

async function snapshot(page) {
  return page.evaluate(() => {
    const button = document.querySelector('#saveProvider');
    return {
      disabled: Boolean(button && button.disabled),
      text: button ? button.textContent.trim() : '',
      error: document.querySelector('#providerError')?.textContent.trim() || '',
      dialogOpen: Boolean(document.querySelector('#providerDialog')?.open),
    };
  });
}

async function cardNames(page) {
  return page.evaluate(() => [...document.querySelectorAll('#providerCards .provider-card[data-provider-card]')]
    .map((card) => card.querySelector('.provider-card-name')?.textContent.trim() || ''));
}

async function waitForClosed(page, timeout = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const open = await page.evaluate(() => Boolean(document.querySelector('#providerDialog')?.open));
    if (!open) return true;
    await page.waitForTimeout(200);
  }
  return false;
}

async function fillForm(page, { withLimits }) {
  await page.fill('#providerName', PREFIX);
  await page.fill('#providerBaseUrl', 'https://example.invalid/v1');
  await page.selectOption('#providerModel', '__custom__');
  await page.fill('#providerModelCustom', 'smoke-model-reentry');
  if (withLimits) {
    // 填全上下文/输出上限 ⇒ saveProvider 跳过慢探测走快路径（用于 ③ 之后的连点验证）。
    await page.fill('#providerContextWindow', '32000');
    await page.fill('#providerMaxOutputTokens', '4096');
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  // 隔离实例首启会弹引导向导；本脚本只关心保存链路。
  await context.addInitScript(() => {
    try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) { /* 无存储权限时忽略 */ }
  });
  const page = await context.newPage();
  const pageErrors = [];
  const badResponses = [];
  // 保存请求的**唯一真相**：不数按钮、不数 toast，直接数发出去的 POST。
  const savePosts = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    // 「Failed to load resource」不带 URL，无法与已知噪音区分（与 context_reset_lock.cjs 同口径）。
    if (msg.type() === 'error' && !msg.text().includes('Failed to load resource')) {
      pageErrors.push(`console.error: ${msg.text()}`);
    }
  });
  page.on('response', (res) => {
    if (res.status() >= 400 && !KNOWN_NOISE.test(res.url())) badResponses.push(`${res.status()} ${res.url()}`);
  });
  page.on('request', (req) => {
    if (req.method() === 'POST' && /\/api\/providers$/.test(req.url())) savePosts.push(req.url());
  });
  page.on('dialog', (dialog) => dialog.accept());
  await page.route('**/api/providers/models', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 900));
    await route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'smoke: 占位供应商不可达' }),
    });
  });

  try {
    await cleanupSeeded();
    const before = (await apiJson('/api/bootstrap')).model_profiles.length;

    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 30000 });
    await page.waitForSelector('#messageInput', { timeout: 30000 });
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="models"]');
    await page.waitForTimeout(400);

    // ─────────── ①② 慢探测 + 连点（只应发出 1 次保存） ───────────
    await page.click('[data-provider-add]');
    await page.waitForTimeout(300);
    await fillForm(page, { withLimits: false });
    const idBefore = await page.inputValue('#providerId');
    check('新建表单不带 id（正是事故那条"无 id 新建"路径）', idBefore === '', idBefore);

    // 进度提示会被后续状态覆盖（自动模型检查等），所以用 MutationObserver 记录全部文案，
    // 断言"曾经出现过"——比肉眼抓某一瞬间稳。
    await page.evaluate(() => {
      window.__errLog = [];
      const box = document.querySelector('#providerError');
      window.__errLog.push(box.textContent.trim());
      new MutationObserver(() => window.__errLog.push(box.textContent.trim()))
        .observe(box, { childList: true, characterData: true, subtree: true });
    });

    // 连点 3 次：第 1 次真点；后两次模拟"按钮已禁用后仍在输入框里按回车"（表单 submit）。
    await page.evaluate(() => {
      const form = document.querySelector('#providerForm');
      const button = document.querySelector('#saveProvider');
      button.click();
      for (let i = 0; i < 2; i += 1) {
        form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
      }
    });
    await page.waitForTimeout(300); // 仍在那 900ms 的探测等待窗口里

    let seen = await snapshot(page);
    check('① 保存中：按钮禁用且文案为「保存中…」',
      seen.disabled && seen.text === '保存中…', JSON.stringify(seen));
    const errLog = await page.evaluate(() => window.__errLog);
    check('① 慢探测阶段有进度提示（不再是"点了没反应"）',
      errLog.some((text) => text.includes('正在获取模型上下文参数')), JSON.stringify(errLog));
    await page.screenshot({ path: path.join(SHOTS, 'provider_save_reentry_1_saving.png') });

    const closed = await waitForClosed(page);
    seen = await snapshot(page);
    check('② 保存完成后弹层关闭', closed === true && seen.dialogOpen === false, JSON.stringify(seen));
    check('② 按钮恢复到原文案（不是永久禁用）',
      seen.disabled === false && seen.text === '保存设置', JSON.stringify(seen));
    check('② 连点 3 次只发出 1 次保存请求', savePosts.length === 1, String(savePosts.length));
    const afterNames = await cardNames(page);
    check('② 列表里只有 1 张本次新建的卡（无重复卡）',
      afterNames.filter((name) => name === PREFIX).length === 1, JSON.stringify(afterNames));
    check('② 卡片总数正好 +1',
      afterNames.length === before + 1, JSON.stringify({ before, after: afterNames.length }));
    await page.screenshot({ path: path.join(SHOTS, 'provider_save_reentry_2_single_card.png') });

    // ─────────── ③ 后端幂等兜底（绕过 UI，直接连发无 id 的 POST） ───────────
    const direct = {
      kind: 'online', name: `${PREFIX}直连`, base_url: 'https://example.invalid/v1',
      model: 'smoke-model-direct', api_key: 'sk-smoke-direct', request_format: 'openai_chat',
    };
    const ids = [];
    for (let i = 0; i < 3; i += 1) {
      const saved = await apiJson('/api/providers', { method: 'POST', body: direct });
      ids.push(saved.id);
    }
    check('③ 无 id 连发 3 次拿到同一个 id（幂等）',
      new Set(ids).size === 1 && Boolean(ids[0]), JSON.stringify(ids));
    const other = await apiJson('/api/providers', {
      method: 'POST', body: { ...direct, api_key: 'sk-smoke-direct-2' },
    });
    check('③ 不同 API Key（多账号）仍允许并存', Boolean(other.id) && other.id !== ids[0],
      JSON.stringify({ first: ids[0], other: other.id }));
    const profiles = (await apiJson('/api/bootstrap')).model_profiles;
    // 3 条 = ①UI 存的 1 条 + ③直连的 2 条（两个不同 Key 允许并存，同名不合并）。
    check('③ 后端按「同名同址同模型同 Key」收口：连发 3 次只落 1 条',
      names(profiles).length === 3, JSON.stringify(names(profiles)));
    // 刷新页面重新读库：卡片数与落库一致，证明不是前端残影。
    await page.reload({ waitUntil: 'load', timeout: 30000 });
    await page.waitForSelector('#messageInput', { timeout: 30000 });
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="models"]');
    await page.waitForTimeout(500);
    const reloaded = (await cardNames(page)).filter(seeded);
    check('③ 刷新后卡片数与落库一致（不是前端假象）',
      reloaded.length === 3, JSON.stringify(reloaded));

    check('零 pageerror / 非白名单 4xx',
      pageErrors.length === 0 && badResponses.length === 0,
      [...pageErrors, ...badResponses].slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.stack ? error.stack.split('\n')[0] : error));
  } finally {
    await cleanupSeeded().catch(() => {});
    await browser.close();
  }

  console.log();
  console.log(`「保存设置」防重入冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
