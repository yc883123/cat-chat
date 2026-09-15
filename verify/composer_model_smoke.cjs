// 会话模型下拉冒烟（由 composer_model_smoke.py 起独立源码 server 后调用）。
// 覆盖：目录返回空列表不算「已缓存」/ 会话内「↻ 重新检测模型」强制重拉并还原原选择 /
//       主动刷新失败要出声 / 会话没有明确选择时显示占位项而不是静默选中目录第一项 /
//       可搜索浮层（浮层挂 body、输入即过滤、↑↓ + Enter 全键盘选择、Esc 关闭并把焦点还给
//       触发按钮、点外部收起、底部刷新仍是动作不落库、375px 窄屏不产生横向滚动）。
// 原生 select 仍是唯一值来源，只是被 CSS 隐藏（1×1 + opacity:0），所以上面的
// page.selectOption / el.options 系列断言依旧直接驱动它。
//
// 同步口径：`saveModelSelection` 的最后一行是 `await populateComposerModels('')` 之后的
// `toast('API 已切换')`——看到这条 toast 即代表这一次切 API 的目录请求已完成、DOM 已重绘。
// 不靠 sleep 猜时序，否则会读到上一次切 API 留下的旧下拉内容（实测踩过）。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const PROVIDER_A = process.env.NAIBA_SMOKE_PROVIDER_A || 'online:smoke-a';
const PROVIDER_B = process.env.NAIBA_SMOKE_PROVIDER_B || 'online:smoke-b';
const BOUND_MODEL = process.env.NAIBA_SMOKE_BOUND_MODEL || 'smoke-pro';
const REFRESH_VALUE = '__refresh_composer_models__';
const CATALOG = [
  { id: 'smoke-flash', name: 'smoke-flash' },   // 故意把非预期模型排第一（复刻供应商返回顺序）
  { id: 'smoke-pro', name: 'smoke-pro' },
  { id: 'smoke-1-flash', name: 'smoke-1-flash' },
];
// 「中转站没分组」的真实形态：目录一次返回 400+ 个模型（这里 420 条）。
const HUGE = Array.from({ length: 420 }, (_, i) => {
  const id = `huge-${String(i).padStart(3, '0')}`;
  return { id, name: id };
});

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    // 本冒烟故意注入一次 502 目录故障（验证「刷新失败要出声」），浏览器随之打印的
    // "Failed to load resource ... 502" 是这次注入的必然产物，不计为页面错误。
    if (msg.text().includes('status of 502')) return;
    pageErrors.push(`console.error: ${msg.text()}`);
  });

  // 目录请求全部拦截：不真连供应商，只注入可控的目录形态。
  let mode = 'full';            // full | empty | fail | huge
  let requestCount = 0;
  await page.route('**/api/providers/models', async (route) => {
    requestCount += 1;
    if (mode === 'fail') {
      await route.fulfill({
        status: 502, contentType: 'application/json',
        body: JSON.stringify({ error: '冒烟注入的目录故障' }),
      });
      return;
    }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ models: mode === 'empty' ? [] : (mode === 'huge' ? HUGE : CATALOG) }),
    });
  });

  const snapshot = () => page.evaluate(() => {
    const el = document.querySelector('#composerModelSelect');
    if (!el) return null;
    return {
      value: el.value,
      disabled: el.disabled,
      selectedText: el.selectedOptions[0] ? el.selectedOptions[0].textContent.trim() : '',
      options: [...el.options].map((o) => ({ value: o.value, text: o.textContent.trim(), disabled: o.disabled })),
    };
  });
  const toastText = () => page.evaluate(() => (document.querySelector('#toast') || {}).textContent || '');
  const modelOptions = (snap) => (snap ? snap.options.filter((o) => CATALOG.some((m) => m.id === o.value)) : []);
  async function waitFor(fn, timeout = 20000, step = 150) {
    const end = Date.now() + timeout;
    let last = null;
    while (Date.now() < end) {
      last = await fn();
      if (last) return last;
      await page.waitForTimeout(step);
    }
    return last;
  }
  // 诊断行：每步打印目录请求次数与两个下拉框的当前状态（失败时一眼看出卡在哪）。
  async function dump(label) {
    const info = await page.evaluate(() => {
      const api = document.querySelector('#modelSelect');
      const model = document.querySelector('#composerModelSelect');
      return {
        apiValue: api ? api.value : null,
        modelValue: model ? model.value : null,
        options: model ? [...model.options].map((o) => o.value) : [],
      };
    });
    console.log(`  · ${label} | requests=${requestCount} mode=${mode} api=${info.apiValue}`
      + ` model=${JSON.stringify(info.modelValue)} options=${JSON.stringify(info.options)}`);
  }
  const conversationModelName = () => page.evaluate(async () => {
    const payload = await fetch('/api/conversations').then((r) => r.json());
    return payload.conversations && payload.conversations.length
      ? String(payload.conversations[0].model_name || '') : null;
  });
  // 切顶栏 API 并等到本次切换彻底落地（见文件头「同步口径」）。
  async function switchApi(value) {
    await page.evaluate(() => { document.querySelector('#toast').textContent = ''; });
    await page.selectOption('#modelSelect', value);
    return (await waitFor(async () => ((await toastText()) === 'API 已切换' ? true : null))) === true;
  }
  // 等目录请求次数增长（切 API 的目录请求是处理器里的后续步骤，不能立即断言）。
  async function waitForRequestGrowth(before) {
    await waitFor(() => (requestCount > before ? true : null), 15000);
    return requestCount - before;
  }

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#composerModelSelect', { timeout: 20000 });

    // ---- ① 目录就绪后的默认选中：会话已保存的模型，而不是目录第一项 ----
    const loaded = await waitFor(async () => {
      const snap = await snapshot();
      return snap && modelOptions(snap).length === CATALOG.length ? snap : null;
    });
    check('目录返回后下拉列出全部模型', !!loaded && modelOptions(loaded).length === 3,
      JSON.stringify(loaded && modelOptions(loaded)));
    check('会话已保存的模型被选中（不是目录第一项）', !!loaded && loaded.value === BOUND_MODEL,
      JSON.stringify(loaded && { value: loaded.value, text: loaded.selectedText }));
    const last = loaded ? loaded.options[loaded.options.length - 1] : null;
    check('下拉末尾固定一项「↻ 重新检测模型」', !!last && last.value === REFRESH_VALUE
      && last.text.includes('重新检测模型'), JSON.stringify(last));

    // ---- ② 「↻ 重新检测模型」：强制重拉 + 还原原选择 + 不落库 ----
    let before = requestCount;
    await page.selectOption('#composerModelSelect', REFRESH_VALUE);
    check('会话内主动刷新会强制重拉目录（绕过缓存）', (await waitForRequestGrowth(before)) === 1,
      `+${requestCount - before}`);
    await waitFor(async () => {
      const snap = await snapshot();
      return snap && !snap.disabled ? snap : null;
    });
    const refreshed = await snapshot();
    check('刷新后还原刷新前的选中模型', refreshed.value === BOUND_MODEL,
      JSON.stringify({ value: refreshed.value, text: refreshed.selectedText }));
    check('刷新完成给出成功提示', (await toastText()).includes('已重新检测到 3 个模型'),
      await toastText());
    check('伪选项没有被写进会话模型', (await conversationModelName()) === BOUND_MODEL,
      String(await conversationModelName()));

    // ---- ③ 主动刷新失败必须出声，且不破坏已有目录 ----
    mode = 'fail';
    before = requestCount;
    await page.selectOption('#composerModelSelect', REFRESH_VALUE);
    await waitForRequestGrowth(before);
    await waitFor(async () => {
      const snap = await snapshot();
      return snap && !snap.disabled ? snap : null;
    });
    check('刷新失败在界面上出声（不静默）', (await toastText()).includes('无法获取模型目录'),
      await toastText());
    check('刷新失败不破坏已有目录与选择', (await snapshot()).value === BOUND_MODEL,
      JSON.stringify(await snapshot()));

    // ---- ④ 空目录不算「已查过」：换个 API 再切回来必须重拉 ----
    mode = 'empty';
    let mark = requestCount;
    check('切到 B（saveModelSelection 完成）', await switchApi(PROVIDER_B));
    check('切到 B 会拉一次目录（B 尚无缓存）', requestCount === mark + 1, `+${requestCount - mark}`);
    const emptySnap = await snapshot();
    await dump('切到 B（目录为空）');
    check('目录为空时只显示「请先在设置中检查模型」占位', emptySnap.value === ''
      && emptySnap.selectedText.includes('请先在设置中检查模型'),
      JSON.stringify({ value: emptySnap.value, text: emptySnap.selectedText }));

    mode = 'full';
    mark = requestCount;
    await switchApi(PROVIDER_A);
    const backToA = await snapshot();
    await dump('切回 A（目录已缓存）');
    check('目录非空时切 API 不重复请求（缓存仍生效）', requestCount === mark, `+${requestCount - mark}`);
    check('切回 A 立即用已缓存目录重绘', modelOptions(backToA).length === 3,
      JSON.stringify(modelOptions(backToA)));

    mark = requestCount;
    await switchApi(PROVIDER_B);
    await dump('再切到 B（空缓存必须重拉）');
    check('空目录不算已缓存：切回来必须重拉（模型后上架也能刷出来）', requestCount === mark + 1,
      `${mark} -> ${requestCount}`);
    const recovered = await snapshot();
    check('目录就绪后仍不静默选中第一项（占位待选）', recovered.value === ''
      && recovered.selectedText.includes('请选择模型'),
      JSON.stringify({ value: recovered.value, text: recovered.selectedText }));
    check('占位项是禁用的空值项', recovered.options[0].value === ''
      && recovered.options[0].disabled === true, JSON.stringify(recovered.options[0]));

    // ---- ⑤ 未选模型时发送被拦下；显式选择后才落库 ----
    await page.fill('#messageInput', '冒烟：未选模型不应发出');
    await page.click('#sendButton');
    await page.waitForTimeout(400);
    check('未选模型时发送被拦下并给出提示', (await toastText()).includes('模型'), await toastText());
    check('被拦下时不产生消息', (await page.locator('#messages .message-row').count()) === 0,
      String(await page.locator('#messages .message-row').count()));
    await page.fill('#messageInput', '');
    await page.selectOption('#composerModelSelect', 'smoke-flash');
    const persisted = await waitFor(async () => ((await conversationModelName()) === 'smoke-flash'));
    check('显式选择的模型落库到会话', persisted === true, String(await conversationModelName()));

    // ---- ⑥ 可搜索下拉：过滤 / ↑↓ + Enter / Esc / 底部「↻ 重新检测模型」 ----
    // 原生 select 已降级为「隐藏的值容器」，可视化层是触发按钮 + 搜索浮层。
    const picker = () => page.evaluate(() => {
      const panel = document.querySelector('#composerModelPanel');
      const trigger = document.querySelector('#composerModelTrigger');
      const search = document.querySelector('#composerModelSearch');
      const empty = document.querySelector('#composerModelEmpty');
      const active = panel ? panel.querySelector('.is-active') : null;
      return {
        panelHidden: panel ? panel.hidden : null,
        panelInBody: panel ? panel.parentElement === document.body : null,
        expanded: trigger ? trigger.getAttribute('aria-expanded') : null,
        triggerText: (document.querySelector('#composerModelTriggerText') || {}).textContent || '',
        triggerDisabled: trigger ? trigger.disabled : null,
        searchFocused: search ? document.activeElement === search : null,
        optionTexts: panel ? [...panel.querySelectorAll('[data-model-index]')].map((el) => el.textContent.trim()) : [],
        activeText: active ? active.textContent.trim() : '',
        emptyShown: empty ? !empty.hidden : null,
        count: (document.querySelector('#composerModelCount') || {}).textContent || '',
        selected: (document.querySelector('#composerModelSelect') || {}).value,
      };
    });
    const waitPickerOpen = () => waitFor(async () => {
      const snap = await picker();
      return snap.panelHidden === false ? snap : null;
    }, 8000);
    const waitForOptions = (n) => waitFor(async () => {
      const snap = await picker();
      return snap.optionTexts.length === n ? snap : null;
    }, 5000);
    // 弹层必须始终紧贴触发按钮（间隔 8px，朝上/朝下贴合都算）且完全在视口内。
    // 400+ 条时面板比按钮上方还高、过滤后又从 426 缩到 ~134——只算 top 会把面板留在原地，
    // 于是按钮上方空出一大截 =「弹层到处飘」（实测踩过，锚底边修掉了）。
    const hug = () => page.evaluate(() => {
      const panel = document.querySelector('#composerModelPanel');
      const trigger = document.querySelector('#composerModelTrigger');
      const p = panel.getBoundingClientRect();
      const t = trigger.getBoundingClientRect();
      return {
        gapAbove: Math.round(t.top - p.bottom),
        gapBelow: Math.round(p.top - t.bottom),
        inside: p.top >= 0 && p.bottom <= window.innerHeight && p.left >= 0 && p.right <= window.innerWidth,
        height: Math.round(p.height),
      };
    });
    const hugsTrigger = (h) => Math.abs(h.gapAbove - 8) <= 12 || Math.abs(h.gapBelow - 8) <= 12;

    check('触发按钮显示当前模型名', (await picker()).triggerText === 'smoke-flash', (await picker()).triggerText);

    await page.click('#composerModelTrigger');
    let pk = await waitPickerOpen();
    check('点击触发按钮展开搜索浮层', !!pk, JSON.stringify(pk && pk.panelHidden));
    check('浮层挂到 body（fixed 定位，躲开 composer 的 overflow 裁剪）', pk.panelInBody === true, String(pk.panelInBody));
    check('展开时 aria-expanded=true 且搜索框自动聚焦', pk.expanded === 'true' && pk.searchFocused === true,
      JSON.stringify({ expanded: pk.expanded, focused: pk.searchFocused }));
    check('浮层列出目录里的全部模型', JSON.stringify(pk.optionTexts) === JSON.stringify(['smoke-flash', 'smoke-pro', 'smoke-1-flash']),
      JSON.stringify(pk.optionTexts));
    check('浮层不出现空值占位项与「↻ 重新检测模型」（各有独立入口）',
      !pk.optionTexts.some((text) => text.includes('请选择模型') || text.includes('重新检测模型')), JSON.stringify(pk.optionTexts));
    const hugFull = await hug();
    check('展开后弹层紧贴触发按钮且不出视口', hugsTrigger(hugFull) && hugFull.inside, JSON.stringify(hugFull));

    // 400+ 条模型的核心诉求：输入关键字即收敛，不再靠滚动找。
    await page.fill('#composerModelSearch', 'flash');
    pk = await waitForOptions(2);
    check('输入关键字后候选收敛到匹配项', !!pk
      && JSON.stringify(pk.optionTexts) === JSON.stringify(['smoke-flash', 'smoke-1-flash']),
      JSON.stringify(pk && pk.optionTexts));
    check('计数显示「过滤后 / 总数」', pk && pk.count === '2/3', pk && pk.count);
    const hugFiltered = await hug();
    check('过滤变短后弹层仍紧贴触发按钮（不会飘到别处）',
      hugsTrigger(hugFiltered) && hugFiltered.inside && hugFiltered.height < hugFull.height,
      JSON.stringify({ full: hugFull, filtered: hugFiltered }));

    await page.fill('#composerModelSearch', 'zzz-不存在');
    const noMatch = await waitFor(async () => {
      const snap = await picker();
      return snap.emptyShown === true ? snap : null;
    }, 5000);
    check('无匹配时列表清空并给出空态', !!noMatch && noMatch.optionTexts.length === 0,
      JSON.stringify(noMatch && { options: noMatch.optionTexts, emptyShown: noMatch.emptyShown }));

    // ↑↓ + Enter：全键盘选择，不碰鼠标。
    await page.fill('#composerModelSearch', 'flash');
    await waitForOptions(2);
    await page.press('#composerModelSearch', 'ArrowDown');
    check('↓ 把高亮移到下一项', (await picker()).activeText === 'smoke-1-flash', (await picker()).activeText);
    await page.press('#composerModelSearch', 'Enter');
    const picked = await waitFor(async () => {
      const snap = await picker();
      return snap.panelHidden === true ? snap : null;
    }, 5000);
    check('Enter 选中高亮项并收起浮层', !!picked && picked.selected === 'smoke-1-flash', JSON.stringify(picked && picked.selected));
    check('触发按钮文案跟到新模型', !!picked && picked.triggerText === 'smoke-1-flash', picked && picked.triggerText);
    check('键盘选择同样落库到会话',
      (await waitFor(async () => ((await conversationModelName()) === 'smoke-1-flash'))) === true,
      String(await conversationModelName()));

    // 重新打开：光标必须落在「当前已选模型」上（400+ 条时不至于一按 Enter 就换成目录第一项）。
    await page.click('#composerModelTrigger');
    await waitPickerOpen();
    check('重新打开时光标落在当前已选模型上', (await picker()).activeText === 'smoke-1-flash',
      (await picker()).activeText);

    // Esc：收起浮层并把焦点还给触发按钮。
    await page.press('#composerModelSearch', 'Escape');
    await waitFor(async () => ((await picker()).panelHidden === true ? true : null), 5000);
    check('Esc 收起浮层', (await picker()).panelHidden === true, JSON.stringify(await picker()));
    check('Esc 后焦点回到触发按钮',
      await page.evaluate(() => document.activeElement === document.querySelector('#composerModelTrigger')), '');

    // 点浮层外部同样收起。
    await page.click('#composerModelTrigger');
    await waitPickerOpen();
    await page.click('#runtimeStatus');
    await waitFor(async () => ((await picker()).panelHidden === true ? true : null), 5000);
    check('点击浮层外部即收起', (await picker()).panelHidden === true, JSON.stringify(await picker()));

    // 底部「↻ 重新检测模型」：仍是动作——强制重拉，但绝不落库。
    let mark2 = requestCount;
    await page.click('#composerModelTrigger');
    await waitPickerOpen();
    await page.click('#composerModelRefresh');
    check('浮层底部刷新强制重拉目录（绕过缓存）', (await waitForRequestGrowth(mark2)) === 1,
      `+${requestCount - mark2}`);
    const refreshSettled = await waitFor(async () => {
      const snap = await picker();
      return snap.panelHidden === true && snap.triggerDisabled === false ? snap : null;
    }, 8000);
    check('刷新完成后收起浮层且触发按钮恢复可点', !!refreshSettled, JSON.stringify(refreshSettled));
    check('浮层里的刷新仍是动作：不写进会话模型',
      (await conversationModelName()) === 'smoke-1-flash', String(await conversationModelName()));
    check('刷新后触发按钮仍显示刷新前的模型', (await picker()).triggerText === 'smoke-1-flash',
      (await picker()).triggerText);

    // 窄屏：浮层必须夹在视口内，且不撑出横向滚动。
    await page.setViewportSize({ width: 375, height: 720 });
    await page.click('#composerModelTrigger');
    await waitPickerOpen();
    const narrow = await page.evaluate(() => {
      const rect = document.querySelector('#composerModelPanel').getBoundingClientRect();
      return {
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
        panelLeft: Math.round(rect.left),
        panelRight: Math.round(rect.right),
      };
    });
    check('375px 窄屏浮层仍夹在视口内', narrow.panelLeft >= 0 && narrow.panelRight <= narrow.innerWidth,
      JSON.stringify(narrow));
    check('375px 窄屏打开浮层不产生横向滚动', narrow.scrollWidth <= narrow.innerWidth, JSON.stringify(narrow));
    await page.press('#composerModelSearch', 'Escape');
    await waitFor(async () => ((await picker()).panelHidden === true ? true : null), 5000);

    // ---- ⑦ 400+ 个模型（中转站未分组）下的真实诉求：搜得到的、不再靠滚动 ----
    await page.setViewportSize({ width: 1280, height: 900 });
    mode = 'huge';
    const hugeBefore = requestCount;
    await page.click('#composerModelTrigger');
    await waitPickerOpen();
    await page.click('#composerModelRefresh');
    check('400+ 目录也能正常重拉', (await waitForRequestGrowth(hugeBefore)) === 1, `+${requestCount - hugeBefore}`);
    // 刷新期间 select 被禁用 → 浮层按设计收起；等刷新落地（触发按钮恢复可点）后再重新打开看目录。
    await waitFor(async () => {
      const snap = await picker();
      return snap.triggerDisabled === false && snap.panelHidden === true ? snap : null;
    }, 20000);
    await page.click('#composerModelTrigger');
    await waitPickerOpen();
    const hugeReady = await waitFor(async () => {
      const snap = await picker();
      return snap.optionTexts.length >= HUGE.length ? snap : null;
    }, 10000);
    const hugeCount = hugeReady ? hugeReady.optionTexts.length : 0;
    check(`400+ 目录时浮层列出全部候选（实测 ${hugeCount} 条）`, hugeCount >= HUGE.length, String(hugeCount));
    check('400+ 条里不混入占位项与刷新伪选项',
      hugeCount > 0 && !hugeReady.optionTexts.some((text) => text.includes('请选择模型') || text.includes('重新检测模型')),
      '');
    const hugHuge = await hug();
    check('400+ 条（面板比按钮上方还高）时弹层仍紧贴触发按钮',
      hugsTrigger(hugHuge) && hugHuge.inside, JSON.stringify(hugHuge));
    const hugeStarted = Date.now();
    await page.fill('#composerModelSearch', 'huge-31');
    const narrowedHuge = await waitFor(async () => {
      const snap = await picker();
      return snap.optionTexts.length > 0 && snap.optionTexts.length <= 20 ? snap : null;
    }, 8000);
    const filterMs = Date.now() - hugeStarted;
    check('输入关键字把 400+ 收敛到少量候选', !!narrowedHuge, JSON.stringify(narrowedHuge && narrowedHuge.optionTexts));
    check(`400+ 下过滤响应足够快（${filterMs}ms，含 150ms 轮询粒度）`, filterMs < 2000, `${filterMs}ms`);
    check('收敛结果确实只含匹配项',
      !!narrowedHuge && narrowedHuge.optionTexts.every((text) => text.includes('huge-31')),
      JSON.stringify(narrowedHuge && narrowedHuge.optionTexts));
    // 全键盘选中 400+ 目录里的一项并落库。
    await page.press('#composerModelSearch', 'Enter');
    const hugePicked = await waitFor(async () => {
      const snap = await picker();
      return snap.panelHidden === true ? snap : null;
    }, 5000);
    check('400+ 目录下 Enter 也能选中并收起', !!hugePicked && hugePicked.selected === 'huge-310',
      JSON.stringify(hugePicked && hugePicked.selected));
    check('400+ 目录下选择照常落库到会话',
      (await waitFor(async () => ((await conversationModelName()) === 'huge-310'))) === true,
      String(await conversationModelName()));

    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
