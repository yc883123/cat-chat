// 供应商预设（「只填 API Key」）+ 首启引导向导冒烟（真后端）。
//
// 覆盖：
//   ① 空供应商实例启动 → 向导自动弹出，1 步高亮，14 张模板卡（一行三张，可滚动），「还有 …」报总数；
//   ② 选 TE 中转 → 进第 2 步：recap 带域名、引导条三段式 ①②③、有「打开注册页」按钮（不真点）、
//      只留 API Key 一个空（URL/模型随模板走，不显输入框）；无 Key 保存被挡；
//   ③ 填 Key 保存 → 弹层关闭 + 卡片落库（base_url/请求格式/推荐模型/来源 preset_id 全对）；
//   ④ 重开页面不再弹（已有可用供应商）；删掉卡片后仍不弹（跳过状态已记）；
//      清掉跳过标记后重开 → 又弹（判据是「有没有可用供应商」，不是「弹过没」）；
//   ⑤ 设置 →「添加 API」弹层是同一套卡片网格（无下拉死控件），点智谱 GLM 自动回填表单，
//      自定义模板没有注册页按钮，且用户手改过的值保存后不被模板覆盖（中转玩家路径）。
//
// 前置：源码 server 已在 NAIBA_SMOKE_BASE（默认 8799）运行，且**一个可用供应商都没有**
//       （由 verify/provider_presets_smoke.py 起的隔离实例保证）。
// 已知噪音：`/api/providers/models` 会 400（占位供应商 example.invalid 连不通），按 §六 惯例排除。
// 运行：node verify\provider_presets_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const ASSERT_DIR = process.env.NAIBA_SMOKE_SHOTS || '.';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

// 已知取样噪音：隔离实例的占位供应商地址是 https://example.invalid/v1，点开设置 →
// 模型页时前端会去拉模型目录，后端必然连不通回 400。与 agent_help_popover.cjs 同一口径
// （§六 已登记），但这里要求「噪音条数」与「被排除的控制台报错条数」严格相等——
// 多出来的照旧算失败，免得白名单把真报错一起吞掉。
const KNOWN_NOISE = [/\/api\/providers\/models(\?|$)/];
function isKnownNoise(url) {
  return KNOWN_NOISE.some((re) => re.test(url));
}

async function apiJson(path, options = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(`${BASE}${path}`, init);
  return response.json().catch(() => ({}));
}

async function wizardSnapshot(page) {
  return page.evaluate(() => {
    const dialog = document.querySelector('#onboardingDialog');
    const grid = document.querySelector('#onboardingPresetGrid');
    const guide = document.querySelector('#onboardingGuide');
    const keyButton = document.querySelector('#onboardingKeyUrl');
    const style = grid ? getComputedStyle(grid) : null;
    const cards = grid ? [...grid.querySelectorAll('[data-provider-preset]')] : [];
    // 步骤条只靠颜色区分「现在第几步」（`.is-active` = accent 色 + 加粗，无下划线），
    // 所以光断言类名不够——把计算色一并读回来，断言「激活那一步的颜色确实与另一步不同」。
    const stepColor = (selector) => {
      const el = document.querySelector(selector);
      return el ? getComputedStyle(el).color : '';
    };
    return {
      open: Boolean(dialog && dialog.open),
      stepPresetActive: Boolean(document.querySelector('#onboardingStepPreset')?.classList.contains('is-active')),
      stepKeyActive: Boolean(document.querySelector('#onboardingStepKey')?.classList.contains('is-active')),
      stepPresetColor: stepColor('#onboardingStepPreset'),
      stepKeyColor: stepColor('#onboardingStepKey'),
      cardCount: cards.length,
      firstCardName: cards[0]?.querySelector('.provider-preset-name')?.textContent.trim() || '',
      lastCardName: cards[cards.length - 1]?.querySelector('.provider-preset-name')?.textContent.trim() || '',
      columns: style ? style.gridTemplateColumns.split(' ').filter(Boolean).length : 0,
      scrollable: grid ? grid.scrollHeight > grid.clientHeight + 2 : false,
      moreText: document.querySelector('#onboardingPresetMore')?.textContent.trim() || '',
      recap: document.querySelector('#onboardingRecap')?.textContent.trim() || '',
      guideVisible: Boolean(guide && !guide.hidden),
      guideText: document.querySelector('#onboardingGuideText')?.textContent.trim() || '',
      keyButtonVisible: Boolean(keyButton && !keyButton.hidden),
      keyButtonPreset: keyButton?.dataset.presetId || '',
      urlHidden: Boolean(document.querySelector('#onboardingUrlField')?.hidden),
      urlValue: document.querySelector('#onboardingBaseUrl')?.value || '',
      keyHidden: Boolean(document.querySelector('#onboardingKeyField')?.hidden),
      modelHidden: Boolean(document.querySelector('#onboardingModelField')?.hidden),
      modelValue: document.querySelector('#onboardingModel')?.value || '',
      saveVisible: Boolean(document.querySelector('#onboardingSave') && !document.querySelector('#onboardingSave').hidden),
      error: document.querySelector('#onboardingError')?.textContent.trim() || '',
      legacySelect: Boolean(document.querySelector('select#providerPreset')),
    };
  });
}

async function dialogSnapshot(page) {
  return page.evaluate(() => {
    const grid = document.querySelector('#providerPresetGrid');
    const guide = document.querySelector('#providerPresetGuide');
    const keyButton = document.querySelector('#providerPresetKeyUrl');
    const cards = grid ? [...grid.querySelectorAll('[data-provider-preset]')] : [];
    return {
      open: Boolean(document.querySelector('#providerDialog')?.open),
      title: document.querySelector('#providerDialogTitle')?.textContent.trim() || '',
      cardCount: cards.length,
      activeCard: cards.find((card) => card.classList.contains('is-active'))?.dataset.providerPreset || '',
      guideVisible: Boolean(guide && !guide.hidden),
      guideText: document.querySelector('#providerPresetGuideText')?.textContent.trim() || '',
      keyButtonVisible: Boolean(keyButton && !keyButton.hidden),
      keyButtonPreset: keyButton?.dataset.presetId || '',
      name: document.querySelector('#providerName')?.value || '',
      baseUrl: document.querySelector('#providerBaseUrl')?.value || '',
      format: document.querySelector('#providerFormat')?.value || '',
      model: document.querySelector('#providerModel')?.value || '',
    };
  });
}

async function providers() {
  const bootstrap = await apiJson('/api/bootstrap');
  return bootstrap.model_profiles || bootstrap.providers || [];
}

// 轮询等待：fn 一律传 async 函数，必须 await。
// ⚠ 不要写成 `fn.await ? await fn() : fn()`——Function 上没有 `await` 属性，判定恒为假，
// 于是拿到的是「未决的 Promise」而不是结果，而 Promise 恒真 ⇒ 等待立即返回 true，
// 断言全绿却是空转（本脚本初版踩过：向导 157ms「就绪」，而当时它还没渲染）。
async function waitFor(page, fn, deadlineMs = 6000) {
  const end = Date.now() + deadlineMs;
  while (Date.now() < end) {
    if (await fn()) return true;
    await page.waitForTimeout(200);
  }
  return false;
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const badResponses = []; // 4xx/5xx 的真实 URL：console 里只报「400」看不出是谁
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('response', (res) => { if (res.status() >= 400) badResponses.push(`${res.status()} ${res.url()}`); });
  page.on('dialog', (dialog) => dialog.accept());

  try {
    // ---- ① 空供应商启动 → 向导自动弹出 ----
    // 源码模式是「一个模块一个请求」的裸服务，冷启动首屏要拉 20+ 个 JS + 若干接口，
    // 首轮等 20s（热重载只需 1~2s），别把服务端的冷启动当成「向导没弹」。
    const startedAt = Date.now();
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    const booted = await waitFor(page, async () => Boolean(await page.evaluate(() => Boolean(document.querySelector('#onboardingDialog')?.open))), 20000);
    console.log(`      （首屏到向导弹出的耗时：${Date.now() - startedAt} ms）`);
    let wizard = await wizardSnapshot(page);
    check('空供应商时首启向导自动弹出', booted && wizard.open === true, JSON.stringify(wizard));
    check('预设表有 14 张模板卡', wizard.cardCount === 14, String(wizard.cardCount));
    check('第 1 步高亮、第 2 步未高亮', wizard.stepPresetActive && !wizard.stepKeyActive, JSON.stringify(wizard));
    check('第 1 步在视觉上确实与第 2 步不同色（高亮不只是类名）',
      wizard.stepPresetColor !== '' && wizard.stepPresetColor !== wizard.stepKeyColor,
      JSON.stringify({ preset: wizard.stepPresetColor, key: wizard.stepKeyColor }));
    check('模板卡一行三张', wizard.columns === 3, String(wizard.columns));
    check('网格两行装不下时是可滚动的（不把 14 张全铺开）', wizard.scrollable === true, String(wizard.scrollable));
    check('「还有 …」提示报出模板总数', wizard.moreText.includes('共 14 个模板'), wizard.moreText);
    check('下拉版死控件已彻底移除', wizard.legacySelect === false, String(wizard.legacySelect));
    check('入口卡是 DeepSeek、末尾是本机 Unsloth（在线在前本地在后）',
      wizard.firstCardName === 'DeepSeek' && wizard.lastCardName === '本机 Unsloth',
      JSON.stringify({ first: wizard.firstCardName, last: wizard.lastCardName }));
    check('第 2 步的内容初始隐藏（先选供应商）',
      wizard.recap === '' && !wizard.guideVisible && wizard.saveVisible === false, JSON.stringify(wizard));

    // ---- ② 选 TE 中转 ----
    await page.click('#onboardingPresetGrid [data-provider-preset="te"]');
    await page.waitForTimeout(300);
    wizard = await wizardSnapshot(page);
    check('选模板后进第 2 步', wizard.stepKeyActive && !wizard.stepPresetActive, JSON.stringify(wizard));
    check('第 2 步高亮后颜色方向随之翻转（不是两步同色）',
      wizard.stepKeyColor !== wizard.stepPresetColor,
      JSON.stringify({ preset: wizard.stepPresetColor, key: wizard.stepKeyColor }));
    check('recap 写明已选供应商与域名', wizard.recap.includes('TE 中转') && wizard.recap.includes('teynex.com'), wizard.recap);
    check('引导条三段式（①注册 ②充值 ③令牌建 Key）',
      wizard.guideVisible && wizard.guideText.includes('①') && wizard.guideText.includes('②') && wizard.guideText.includes('③'),
      wizard.guideText);
    check('引导条写明 403 预扣费风险与 codex 切法',
      wizard.guideText.includes('max_tokens') && wizard.guideText.includes('codex_responses'), wizard.guideText);
    check('「打开注册页」按钮出现且指向 TE 预设',
      wizard.keyButtonVisible && wizard.keyButtonPreset === 'te', JSON.stringify({ v: wizard.keyButtonVisible, p: wizard.keyButtonPreset }));
    check('正常路径只剩 API Key 一个空（URL/模型随模板走）',
      wizard.keyHidden === false && wizard.urlHidden === true && wizard.modelHidden === true,
      JSON.stringify({ key: wizard.keyHidden, url: wizard.urlHidden, model: wizard.modelHidden }));
    check('保存按钮已就绪', wizard.saveVisible === true, String(wizard.saveVisible));
    await page.screenshot({ path: `${ASSERT_DIR}/provider_presets_onboarding.png` });

    // 无 Key 保存必须被挡（不能落一张没有 Key 的废卡）
    await page.click('#onboardingSave');
    await page.waitForTimeout(400);
    wizard = await wizardSnapshot(page);
    check('没粘 Key 时保存被挡下并给出原因',
      wizard.open === true && wizard.error.includes('API Key'), JSON.stringify({ open: wizard.open, error: wizard.error }));

    // ---- ③ 填 Key → 保存并开始 ----
    await page.fill('#onboardingApiKey', 'sk-smoke-not-real');
    await page.click('#onboardingSave');
    const closed = await waitFor(page, async () => !(await page.evaluate(() => Boolean(document.querySelector('#onboardingDialog')?.open))));
    check('保存后向导关闭', closed === true, '');
    const saved = await waitFor(page, async () => (await providers()).some((item) => item.preset_id === 'te'));
    const cards = await providers();
    const te = cards.find((item) => item.preset_id === 'te') || {};
    check('TE 卡片已落库', Boolean(saved && te.id), JSON.stringify(cards.map((c) => c.id)));
    check('落库字段来自模板（名称/地址/格式/推荐模型）',
      te.name === 'TE 中转' && te.base_url === 'https://teynex.com' && te.request_format === 'openai_chat' && te.model === 'kimi-k3',
      JSON.stringify({ name: te.name, url: te.base_url, format: te.request_format, model: te.model }));
    check('卡片记住来源模板（下次点开可反显）', te.preset_id === 'te', String(te.preset_id));
    check('API Key 已写入（不显示明文）', te.has_api_key === true && te.api_key === '', JSON.stringify({ has: te.has_api_key, plain: te.api_key }));

    // ---- ④ 重开不再弹；删卡/清标记的行为也要对 ----
    // 「不该弹」的等待时长有自校验：紧接着第 3 次重开会断言「清掉标记后它**确实**弹出来」，
    // 同一个页面、同一段启动序列——那条能过，就说明这两条 2s 的等待窗口足够。
    await page.reload({ waitUntil: 'load' });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(2000);
    wizard = await wizardSnapshot(page);
    check('已有可用供应商 → 重开不再弹向导', wizard.open === false, JSON.stringify(wizard));

    await apiJson(`/api/providers/${encodeURIComponent(te.id)}`, { method: 'DELETE' });
    await page.reload({ waitUntil: 'load' });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(2000);
    wizard = await wizardSnapshot(page);
    check('删掉卡片后仍不弹（跳过/看过已记住）', wizard.open === false, JSON.stringify(wizard));

    await page.evaluate(() => localStorage.removeItem('naibaOnboardingDismissed'));
    await page.reload({ waitUntil: 'load' });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    const popped = await waitFor(page, async () => Boolean(await page.evaluate(() => Boolean(document.querySelector('#onboardingDialog')?.open))));
    check('清掉记忆标记 + 无可用供应商 → 向导重新出现（判据是空态，不是「弹过没」）', popped === true, '');
    await page.click('#onboardingSkip');
    await page.waitForTimeout(300);
    wizard = await wizardSnapshot(page);
    check('「跳过，稍后自己配置」关闭向导', wizard.open === false, JSON.stringify(wizard));

    // ---- ⑤ 设置弹层：同一套网格 + 回填 + 用户值优先 ----
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="models"]');
    await page.waitForTimeout(300);
    await page.click('[data-provider-add]');
    await page.waitForTimeout(500);
    let dialog = await dialogSnapshot(page);
    check('「添加 API」弹层渲染同一套模板网格（14 张）', dialog.open === true && dialog.cardCount === 14, JSON.stringify(dialog));
    check('新卡不预选任何模板（等价自定义配置）', dialog.activeCard === '' && dialog.name === '', JSON.stringify(dialog));
    check('标题与副标题说明「选模板后只填 Key」',
      dialog.title === '添加 API' && dialog.guideText.includes('选择上方任一模板'), JSON.stringify(dialog));

    await page.click('#providerPresetGrid [data-provider-preset="zhipu"]');
    await page.waitForTimeout(300);
    dialog = await dialogSnapshot(page);
    check('点智谱 GLM 自动回填名称/地址/格式/推荐模型',
      dialog.activeCard === 'zhipu' && dialog.name === '智谱 GLM'
      && dialog.baseUrl === 'https://open.bigmodel.cn/api/paas/v4'
      && dialog.format === 'openai_chat' && dialog.model === 'glm-4.6',
      JSON.stringify(dialog));
    check('引导条切到智谱并且有注册页按钮',
      dialog.guideVisible && dialog.guideText.includes('open.bigmodel.cn') && dialog.keyButtonVisible && dialog.keyButtonPreset === 'zhipu',
      JSON.stringify({ text: dialog.guideText.slice(0, 40), visible: dialog.keyButtonVisible }));

    await page.click('#providerPresetGrid [data-provider-preset="bailan"]');
    await page.waitForTimeout(200);
    dialog = await dialogSnapshot(page);
    check('切到摆烂中转后回填为 grok-4.6 / api.bailan.store',
      dialog.activeCard === 'bailan' && dialog.baseUrl === 'https://api.bailan.store' && dialog.model === 'grok-4.6',
      JSON.stringify(dialog));

    await page.click('#providerPresetGrid [data-provider-preset="custom"]');
    await page.waitForTimeout(200);
    dialog = await dialogSnapshot(page);
    check('自定义模板不填地址、也不给注册页按钮',
      dialog.baseUrl === '' && dialog.keyButtonVisible === false, JSON.stringify(dialog));
    await page.screenshot({ path: `${ASSERT_DIR}/provider_presets_dialog.png` });

    // 中转玩家路径：选 TE 模板后把 URL/格式改成 codex 组合，保存后用户值优先、不被模板覆盖
    const custom = await apiJson('/api/providers', {
      method: 'POST',
      body: {
        preset_id: 'te', name: '', base_url: 'https://teynex.com/v1', request_format: 'codex_responses',
        model: 'gpt-5.6-terra', api_key: 'sk-smoke-codex',
      },
    });
    check('选 TE 模板 + 手改 codex 组合：用户值优先',
      custom.base_url === 'https://teynex.com/v1' && custom.request_format === 'codex_responses' && custom.model === 'gpt-5.6-terra',
      JSON.stringify({ url: custom.base_url, format: custom.request_format, model: custom.model }));
    check('手改后仍记住来源模板（供编辑时反显）', custom.preset_id === 'te', String(custom.preset_id));
    const bad = await fetch(`${BASE}/api/providers`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset_id: 'no-such-vendor', name: 'x', base_url: 'https://x.example', request_format: 'openai_chat', model: 'x' }),
    });
    check('未知模板被服务端挡下（400，不是静默建成一张怪卡）', bad.status === 400, String(bad.status));

    await page.click('#cancelProvider');
    await page.waitForTimeout(300);
    // 收尾：清掉本轮建出来的卡片（隔离目录随后整体删除，这里只是保持语义干净）。
    for (const item of await providers()) {
      if (['te', 'bailan', 'custom', 'zhipu'].includes(item.preset_id) || item.name === 'TE 中转') {
        await apiJson(`/api/providers/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
      }
    }

    // 上面那次「未知模板」的 400 是故意打的，它来自 Node 侧 fetch、不回灌页面控制台；
    // 所以这里对页面的要求仍是「除已知取样噪音外，一次 4xx/5xx 都不该有」。
    // 控制台报错文本里不带 URL，用「条数必须正好对上」来保证白名单没吞掉真错误。
    const noiseResponses = badResponses.filter(isKnownNoise);
    const otherResponses = badResponses.filter((item) => !isKnownNoise(item));
    const otherErrors = pageErrors.filter((msg) => !/Failed to load resource/.test(msg));
    const noiseConsole = pageErrors.length - otherErrors.length;
    check('零 pageerror / console.error（取样噪音除外）', otherErrors.length === 0, otherErrors.slice(0, 3).join(' | '));
    check('取样噪音的控制台报错条数与真实 400 条数一致（白名单没吞真错）',
      noiseConsole === noiseResponses.length && otherResponses.length === 0,
      JSON.stringify({ noiseConsole, noiseResponses: noiseResponses.length, other: otherResponses.slice(0, 5) }));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.stack ? error.stack.split('\n')[0] : error));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`供应商预设冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
