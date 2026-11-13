// 真实隔离后端保存；仅模型目录与连接测试使用精确接口替身，不访问外部供应商。
// 覆盖桌面/375px：首启与跳过、自动检测/切换/保存、迟到请求、手填回退、设置模板。
const { chromium } = require('playwright');
const path = require('node:path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const ASSERT_DIR = process.env.NAIBA_SMOKE_SHOTS || __dirname;
const failures = [];
const scriptErrors = [];
const badResponses = [];
const resourceErrors = [];
const expectedFailures = [];
const pending = new Map();
const catalogOverrides = new Map();
const tests = [];
const detectedModels = [{ id: 'detected-first', name: '检测模型一' }, { id: 'detected-chosen', name: '检测模型二' }];

function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

function checkNoScriptError() {
  if (scriptErrors.length) throw new Error(scriptErrors.join(' | '));
}

async function apiJson(url, options = {}) {
  const response = await fetch(`${BASE}${url}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const result = await response.json();
  if (!response.ok) throw new Error(`${url}: ${JSON.stringify(result)}`);
  return result;
}

async function providers() {
  const bootstrap = await apiJson('/api/bootstrap');
  return bootstrap.model_profiles || bootstrap.providers || [];
}

async function preparePage(browser, width) {
  const page = await browser.newPage({ viewport: { width, height: 950 } });
  page.on('pageerror', error => scriptErrors.push(`${width}px: ${error.message}`));
  page.on('console', msg => {
    if (msg.type() !== 'error') return;
    if (/Failed to load resource/.test(msg.text())) resourceErrors.push(msg.text());
    else scriptErrors.push(`${width}px console.error: ${msg.text()}`);
  });
  page.on('response', response => {
    if (response.status() >= 400) badResponses.push(`${response.status()} ${response.url()}`);
  });
  page.on('dialog', dialog => dialog.accept());
  await page.route('**/api/providers/models', async route => {
    const values = route.request().postDataJSON();
    const key = values.api_key || '';
    if (key.startsWith('sk-pending-')) {
      pending.set(key, { route, values });
      return;
    }
    if (key === 'sk-empty') return route.fulfill({ json: { models: [] } });
    if (key === 'sk-failed') {
      expectedFailures.push(`400 ${route.request().url()}`);
      return route.fulfill({ status: 400, json: { error: '离线替身：模型目录暂时不可用' } });
    }
    const models = catalogOverrides.get(key) || (key === 'sk-current'
      ? [{ id: 'fresh-first' }, { id: 'fresh-second' }]
      : detectedModels);
    await route.fulfill({ json: { models } });
  });
  await page.route('**/api/providers/test', async route => {
    tests.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true, response: 'OK', supports_images: false, capability_confirmed: true, capability_source: 'model_catalog' } });
  });
  return page;
}

async function openFreshOnboarding(page) {
  await page.goto(BASE, { waitUntil: 'load', timeout: 30000 });
  await page.waitForSelector('#messageInput', { timeout: 20000 });
  await page.waitForSelector('#onboardingDialog[open]', { timeout: 15000 });
  checkNoScriptError();
}

async function snapshot(page) {
  return page.evaluate(() => {
    const element = id => document.getElementById(id);
    const grid = element('onboardingPresetGrid');
    const style = getComputedStyle(grid);
    return {
      open: element('onboardingDialog').open,
      stepPreset: element('onboardingStepPreset').classList.contains('is-active'),
      stepKey: element('onboardingStepKey').classList.contains('is-active'),
      stepColors: ['onboardingStepPreset', 'onboardingStepKey'].map(id => getComputedStyle(element(id)).color),
      count: grid.querySelectorAll('[data-provider-preset]').length,
      columns: style.gridTemplateColumns.split(' ').filter(Boolean).length,
      scrollable: grid.scrollHeight > grid.clientHeight + 2,
      more: element('onboardingPresetMore').textContent,
      recap: element('onboardingRecap').textContent,
      guide: element('onboardingGuideText').textContent,
      keyButton: !element('onboardingKeyUrl').hidden,
      keyPreset: element('onboardingKeyUrl').dataset.presetId,
      urlHidden: element('onboardingUrlField').hidden,
      modelHidden: element('onboardingModelField').hidden,
      model: element('onboardingModel').value,
      options: [...element('onboardingModel').options].map(option => option.value),
      manual: element('onboardingModelCustom').value,
      status: element('onboardingModelStatus').textContent,
      error: element('onboardingError').textContent,
    };
  });
}

async function waitModel(page, value) {
  await page.waitForFunction(expected => document.getElementById('onboardingModel').value === expected, value);
  checkNoScriptError();
}

async function waitPending(page, key) {
  const deadline = Date.now() + 5000;
  while (!pending.has(key) && Date.now() < deadline) await page.waitForTimeout(30);
  if (!pending.has(key)) throw new Error(`没有收到预期模型请求 ${key}`);
  checkNoScriptError();
}

async function releasePending(key, models) {
  const request = pending.get(key);
  if (!request) throw new Error(`没有可释放的请求 ${key}`);
  pending.delete(key);
  await request.route.fulfill({ json: { models } });
}

async function assertFits(page, label) {
  const overflow = await page.evaluate(() => {
    const dialog = document.getElementById('onboardingDialog');
    return [...dialog.querySelectorAll('input:not([type="hidden"]), select, button')]
      .filter(element => element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden')
      .map(element => ({ id: element.id, rect: element.getBoundingClientRect().toJSON() }))
      .filter(item => item.rect.left < -1 || item.rect.right > innerWidth + 1);
  });
  check(label, overflow.length === 0, JSON.stringify(overflow));
  const selectHeight = await page.locator('#onboardingModel').evaluate(element => element.getBoundingClientRect().height);
  check(`${label}，模型下拉高度为 40px`, selectHeight === 40, String(selectHeight));
  checkNoScriptError();
}

async function runDesktop(browser, presets) {
  const page = await preparePage(browser, 1280);
  await openFreshOnboarding(page);
  let view = await snapshot(page);
  check('空供应商首启自动弹出，第一步高亮', view.open && view.stepPreset && !view.stepKey, JSON.stringify(view));
  check('两步高亮有实际颜色差异', view.stepColors[0] !== view.stepColors[1], JSON.stringify(view.stepColors));
  check('模板数量与后端名单一致，桌面三列并可滚动', view.count === presets.length && view.columns === 3 && view.scrollable, JSON.stringify(view));
  check('更多提示与后端模板总数一致', view.more.includes(`共 ${presets.length} 个模板`), view.more);

  await page.click('#onboardingPresetGrid [data-provider-preset="te"]');
  view = await snapshot(page);
  check('选模板后进入第二步，立即显示空模型选择', view.stepKey && !view.modelHidden && view.model === '', JSON.stringify(view));
  check('供应商和申请 Key 指引保留', view.recap.includes('TE 中转') && view.recap.includes('teynex.com') && view.keyButton && view.keyPreset === 'te' && ['①', '②', '③'].every(text => view.guide.includes(text)), JSON.stringify(view));
  check('大厂/中转沿用模板地址，无需手填', view.urlHidden, JSON.stringify(view));
  await page.click('#onboardingSave');
  check('没有 Key 时保存给出原因', (await snapshot(page)).error.includes('API Key'));
  await page.fill('#onboardingApiKey', 'sk-smoke-list');
  await waitModel(page, 'detected-first');
  view = await snapshot(page);
  check('粘贴 Key 后自动检测并填入接口实际返回的首个模型', view.options.includes('detected-chosen') && view.model === 'detected-first', JSON.stringify(view));
  check('预设推荐模型不会混入实际模型列表', !view.options.includes(presets.find(item => item.id === 'te').model), JSON.stringify(view.options));
  await page.selectOption('#onboardingModel', 'detected-chosen');
  await page.click('#onboardingTest');
  await page.waitForFunction(() => document.getElementById('onboardingError').textContent.includes('连接成功'));
  check('测试连接使用当前选中的模型', tests.at(-1)?.model === 'detected-chosen', JSON.stringify(tests.at(-1)));
  catalogOverrides.set('sk-smoke-list', [{ id: 'refreshed-model' }]);
  await page.click('#onboardingLoadModels');
  await waitModel(page, 'refreshed-model');
  check('重新检测切换模型时清除上一模型的测试成功提示', (await snapshot(page)).error === '');
  catalogOverrides.delete('sk-smoke-list');
  await page.click('#onboardingLoadModels');
  await waitModel(page, 'detected-first');
  await page.selectOption('#onboardingModel', 'detected-chosen');
  checkNoScriptError();
  await assertFits(page, '桌面引导控件位于视口内');
  await page.screenshot({ path: path.join(ASSERT_DIR, 'provider_presets_onboarding.png') });
  await page.click('#onboardingSave');
  await page.waitForSelector('#onboardingDialog[open]', { state: 'hidden' });
  checkNoScriptError();
  const saved = (await providers()).find(item => item.preset_id === 'te');
  check('真实后端保存用户选中的检测模型', saved?.model === 'detected-chosen' && saved.base_url === 'https://teynex.com' && saved.has_api_key && saved.api_key === '', JSON.stringify(saved));
  await page.reload({ waitUntil: 'load' });
  await page.waitForTimeout(1200);
  check('已有可用供应商，刷新不弹引导', !(await snapshot(page)).open);
  await apiJson(`/api/providers/${encodeURIComponent(saved.id)}`, { method: 'DELETE' });
  await page.reload({ waitUntil: 'load' });
  await page.waitForTimeout(1200);
  check('删除供应商后仍记住已经看过引导', !(await snapshot(page)).open);
  await page.evaluate(() => localStorage.removeItem('naibaOnboardingDismissed'));
  await page.reload({ waitUntil: 'load' });
  await page.waitForSelector('#onboardingDialog[open]');
  check('清空跳过标记后，无可用供应商会再次引导', (await snapshot(page)).open);
  checkNoScriptError();

  await page.click('#onboardingPresetGrid [data-provider-preset="custom"]');
  await page.fill('#onboardingBaseUrl', 'https://old.invalid/v1');
  await page.fill('#onboardingApiKey', 'sk-pending-old');
  await waitPending(page, 'sk-pending-old');
  await page.fill('#onboardingApiKey', 'sk-current');
  await waitModel(page, 'fresh-first');
  await releasePending('sk-pending-old', [{ id: 'obsolete-model' }]);
  await page.waitForTimeout(120);
  view = await snapshot(page);
  check('旧 Key 请求迟到不会覆盖新目录和已选模型', view.model === 'fresh-first' && !view.options.includes('obsolete-model'), JSON.stringify(view));
  await page.fill('#onboardingApiKey', 'sk-pending-manual');
  view = await snapshot(page);
  check('修改连接立即清理旧模型目录', !view.options.includes('fresh-first'), JSON.stringify(view));
  await waitPending(page, 'sk-pending-manual');
  await page.selectOption('#onboardingModel', '__custom__');
  await page.fill('#onboardingModelCustom', 'manual-during-detection');
  await releasePending('sk-pending-manual', [{ id: 'late-listed-model' }]);
  await page.waitForTimeout(120);
  view = await snapshot(page);
  check('检测期间改为手填，迟到目录保留手填选择', view.model === '__custom__' && view.manual === 'manual-during-detection', JSON.stringify(view));

  await page.fill('#onboardingApiKey', 'sk-pending-preset');
  await waitPending(page, 'sk-pending-preset');
  await page.click('#onboardingPresetGrid [data-provider-preset="zhipu"]');
  await releasePending('sk-pending-preset', [{ id: 'wrong-preset-model' }]);
  await page.waitForTimeout(120);
  view = await snapshot(page);
  check('切换模板会作废上一模板的探测', view.model === '' && !view.options.includes('wrong-preset-model'), JSON.stringify(view));
  await page.fill('#onboardingApiKey', 'sk-pending-back');
  await waitPending(page, 'sk-pending-back');
  await page.click('#onboardingBack');
  await releasePending('sk-pending-back', [{ id: 'wrong-back-model' }]);
  await page.waitForTimeout(120);
  view = await snapshot(page);
  check('重新选择返回第一步，迟到探测不回填', view.stepPreset && view.model === '' && !view.options.includes('wrong-back-model'), JSON.stringify(view));
  await page.click('#onboardingPresetGrid [data-provider-preset="te"]');
  await page.fill('#onboardingApiKey', 'sk-pending-close');
  await waitPending(page, 'sk-pending-close');
  await page.click('#onboardingDialog [data-close="onboardingDialog"]');
  await releasePending('sk-pending-close', [{ id: 'wrong-closed-model' }]);
  await page.waitForTimeout(120);
  check('关闭引导后迟到探测不改回表单', !(await snapshot(page)).open && (await snapshot(page)).model === '');
  checkNoScriptError();

  await page.click('#openSettings');
  await page.waitForSelector('#settingsDialog[open]');
  await page.click('.settings-nav button[data-settings-tab="models"]');
  await page.click('[data-provider-add]');
  await page.waitForSelector('#providerDialog[open]');
  check('添加 API 仍使用同一套完整模板网格', await page.locator('#providerPresetGrid [data-provider-preset]').count() === presets.length);
  check('添加 API 默认不选模板', await page.locator('#providerName').inputValue() === '');
  for (const id of ['zhipu', 'qianfan']) {
    const preset = presets.find(item => item.id === id);
    await page.click(`#providerPresetGrid [data-provider-preset="${id}"]`);
    const values = await page.evaluate(() => ({ name: document.getElementById('providerName').value, url: document.getElementById('providerBaseUrl').value, format: document.getElementById('providerFormat').value, model: document.getElementById('providerModel').value }));
    check(`设置页 ${preset.name} 沿用模板字段`, values.name === preset.name && values.url === preset.base_url && values.format === preset.request_format && values.model === preset.model, JSON.stringify(values));
    checkNoScriptError();
  }
  await page.click('#providerPresetGrid [data-provider-preset="custom"]');
  check('设置自定义模板不预填地址且无注册按钮', await page.locator('#providerBaseUrl').inputValue() === '' && await page.locator('#providerPresetKeyUrl').isHidden());
  await page.screenshot({ path: path.join(ASSERT_DIR, 'provider_presets_dialog.png') });
  await page.click('#cancelProvider');
  const custom = await apiJson('/api/providers', { method: 'POST', body: { preset_id: 'te', name: '', base_url: 'https://teynex.com/v1', request_format: 'codex_responses', model: 'manual-codex-model', api_key: 'sk-smoke-codex' } });
  check('预设后手改 URL/格式/模型，真实保存值优先', custom.base_url === 'https://teynex.com/v1' && custom.request_format === 'codex_responses' && custom.model === 'manual-codex-model' && custom.preset_id === 'te', JSON.stringify(custom));
  await apiJson(`/api/providers/${encodeURIComponent(custom.id)}`, { method: 'DELETE' });
  const bad = await fetch(`${BASE}/api/providers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ preset_id: 'no-such-vendor', name: 'x', base_url: 'https://x.example', request_format: 'openai_chat', model: 'x' }) });
  check('未知模板仍由真实后端拒绝', bad.status === 400, String(bad.status));
  checkNoScriptError();
  await page.close();
}

async function runMobile(browser, presets) {
  const page = await preparePage(browser, 375);
  await openFreshOnboarding(page);
  const initial = await snapshot(page);
  const gridFits = await page.locator('#onboardingPresetGrid').evaluate(grid => [...grid.querySelectorAll('[data-provider-preset]')].every(card => {
    const rect = card.getBoundingClientRect();
    return rect.left >= 0 && rect.right <= innerWidth;
  }));
  check('375px 全部模板可见且卡片无横向溢出', initial.count === presets.length && gridFits, JSON.stringify(initial));
  await page.click('#onboardingPresetGrid [data-provider-preset="te"]');
  await page.fill('#onboardingApiKey', 'sk-smoke-mobile');
  await waitModel(page, 'detected-first');
  await page.selectOption('#onboardingModel', 'detected-chosen');
  check('375px 可切换检测模型', (await snapshot(page)).model === 'detected-chosen');
  await assertFits(page, '375px 引导、模型选择和操作按钮无横向溢出');
  await page.screenshot({ path: path.join(ASSERT_DIR, 'provider_presets_onboarding_375.png') });
  const themeStyles = await page.evaluate(() => {
    const root = document.documentElement;
    const original = root.dataset.theme;
    const select = document.getElementById('onboardingModel');
    const style = () => ({
      color: getComputedStyle(select).color,
      background: getComputedStyle(select).backgroundColor,
      dialogBackground: getComputedStyle(document.getElementById('onboardingDialog')).backgroundColor,
    });
    root.dataset.theme = 'light';
    const light = style();
    root.dataset.theme = 'dark';
    const dark = style();
    return { original, light, dark };
  });
  check('模型选择随亮暗主题切换文字和背景', themeStyles.light.background !== themeStyles.dark.background && themeStyles.light.color !== themeStyles.dark.color, JSON.stringify(themeStyles));
  check('引导弹窗背景随主题切换，暗色文字不会落在白底上', themeStyles.light.dialogBackground !== themeStyles.dark.dialogBackground && themeStyles.dark.dialogBackground === themeStyles.dark.background, JSON.stringify(themeStyles));
  await page.screenshot({ path: path.join(ASSERT_DIR, 'provider_presets_onboarding_375_dark.png') });
  await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, themeStyles.original);
  await page.fill('#onboardingApiKey', 'sk-empty');
  await page.waitForFunction(() => !document.getElementById('onboardingLoadModels').disabled && document.getElementById('onboardingModelStatus').textContent.length > 0);
  let view = await snapshot(page);
  check('空模型列表不再回退到模板固定模型', view.model === '' && !view.options.includes(presets.find(item => item.id === 'te').model), JSON.stringify(view));
  await page.fill('#onboardingApiKey', 'sk-failed');
  await page.waitForFunction(() => document.getElementById('onboardingModelStatus').textContent.includes('离线替身'));
  await page.selectOption('#onboardingModel', '__custom__');
  await page.fill('#onboardingModelCustom', 'mobile-manual-model');
  await page.click('#onboardingTest');
  await page.waitForFunction(() => document.getElementById('onboardingError').textContent.includes('连接成功'));
  check('探测失败后可手填，测试使用手填模型', tests.at(-1)?.model === 'mobile-manual-model', JSON.stringify(tests.at(-1)));
  checkNoScriptError();
  await assertFits(page, '375px 手填模型与重试控件无横向溢出');
  await page.screenshot({ path: path.join(ASSERT_DIR, 'provider_presets_onboarding_375_manual.png') });
  await page.click('#onboardingSave');
  await page.waitForSelector('#onboardingDialog[open]', { state: 'hidden' });
  const saved = (await providers()).find(item => item.preset_id === 'te');
  check('375px 手填模型经真实后端保存', saved?.model === 'mobile-manual-model', JSON.stringify(saved));
  await apiJson(`/api/providers/${encodeURIComponent(saved.id)}`, { method: 'DELETE' });
  checkNoScriptError();
  await page.close();
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const { presets } = await apiJson('/api/provider-presets');
    if (!presets?.length) throw new Error('真实后端未返回供应商模板');
    await runDesktop(browser, presets);
    await runMobile(browser, presets);
    check('所有关键步骤无 pageerror / 非资源 console.error', scriptErrors.length === 0, scriptErrors.join(' | '));
    check('只有明确模拟的模型探测失败返回 4xx', badResponses.length === expectedFailures.length && badResponses.every((value, index) => value === expectedFailures[index]), JSON.stringify({ badResponses, expectedFailures }));
    check('资源错误条数与预期探测失败一致', resourceErrors.length === expectedFailures.length, JSON.stringify(resourceErrors));
  } catch (error) {
    check('冒烟执行未抛异常', false, error.stack || String(error));
  } finally {
    await browser.close();
  }
  console.log(`供应商预设与新手导航：${failures.length ? `${failures.length} 项失败：${failures.join('、')}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
