// 软件更新「目标版本」下拉回归（源码 server + 拦截 /api/update* 注入固定版本表）。
//
// 覆盖（对应 2.3.9 版本选择器改造）：
//   A. 只渲染可自动更新的版本（2.0.0 及更早被过滤），历史版本全量保留、不被截断为 20 条；
//   B. 展开面板高度锁定为 20 行，超出可滚动，且**不被祖先 overflow 裁剪**
//      （用 elementFromPoint 命中测试；只看 getBoundingClientRect 对裁剪无感）；
//   C. 键盘：↑/↓ 移动选中、Escape / Enter 开关；
//   D. 检查到新版本时默认聚焦最新目标；已是最新时聚焦当前版本；
//   E. 全程零 pageerror / console.error。
//
// 运行：$env:NODE_PATH="<node_modules>"; node verify\update_version_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8798';
// 与 styles.css 的 `.update-version-menu { max-height: calc(20 * 34px) }` 对齐。
// 注意全局 `* { box-sizing: border-box }`，带边框的外框高度才是契约值。
const MENU_BOX_HEIGHT = 20 * 34;
const ROW_MIN_PX = 32;              // `.update-version-option { min-height: 32px }`
const LATEST_PATCH = 26;            // 2.1.26 是最新
const CURRENT_VERSION = '2.1.0';    // 当前版本必须在列表里，否则「聚焦当前版本」无从谈起
// 版本表含 2.1.26 … 2.1.0 共 27 项（> 20：同时验证「可滚动」与「列表不被截断」）。
const TOTAL_VERSIONS = LATEST_PATCH + 1;

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

// 从新到旧的版本表：2.1.26 … 2.1.0（含当前版本），末位再补一个 2.0.0（必须被前端过滤）。
function buildReleases() {
  const releases = [];
  for (let i = LATEST_PATCH; i >= 0; i -= 1) {
    const version = `2.1.${i}`;
    releases.push({
      tag: `v${version}`,
      version,
      published_at: '2026-09-01T00:00:00Z',
      release_url: `https://github.com/yc883123/naiba-chat/releases/tag/v${version}`,
      release_notes: [`${version} 的更新说明`],
      installable: true,
      current: version === CURRENT_VERSION,
    });
  }
  releases.push({
    tag: 'v2.0.0', version: '2.0.0', published_at: '2025-01-01T00:00:00Z',
    release_url: 'https://github.com/yc883123/naiba-chat/releases/tag/v2.0.0',
    release_notes: ['大版本迁移'], installable: true, current: false,
  });
  return releases;
}

function statusPayload(phase) {
  return {
    supported: true,
    mode: 'executable',
    phase,
    current_version: CURRENT_VERSION,
    current_commit: 'c'.repeat(40),
    latest_version: `2.1.${LATEST_PATCH}`,
    releases: buildReleases(),
    checked_at: 1757800000,
  };
}

const snapshot = (page) => page.evaluate(() => {
  const wrap = document.querySelector('#updateVersionCombobox');
  const trigger = document.querySelector('#updateVersionTrigger');
  const label = document.querySelector('#updateVersionLabel');
  const menu = document.querySelector('#updateVersionMenu');
  const native = document.querySelector('#updateVersionSelect');
  const rect = (el) => (el ? el.getBoundingClientRect() : null);
  const style = (el) => (el ? getComputedStyle(el) : null);
  const options = menu ? [...menu.querySelectorAll('[role="option"]')] : [];
  return {
    wrapHidden: wrap ? wrap.hidden : null,
    triggerDisabled: trigger ? trigger.disabled : null,
    triggerText: label ? label.textContent.trim() : '',
    menuHidden: menu ? menu.hidden : null,
    menuBoxHeight: Math.round(rect(menu)?.height || 0),
    menuTop: Math.round(rect(menu)?.top || 0),
    menuBottom: Math.round(rect(menu)?.bottom || 0),
    viewport: window.innerHeight,
    menuClientHeight: menu ? menu.clientHeight : 0,
    menuScrollHeight: menu ? menu.scrollHeight : 0,
    menuMaxHeight: style(menu) ? style(menu).maxHeight : '',
    nativeValue: native ? native.value : '',
    nativeValues: native ? [...native.options].map((o) => o.value) : [],
    optionCount: options.length,
    optionTexts: options.map((n) => n.textContent.trim()),
    selectedText: (options.find((n) => n.getAttribute('aria-selected') === 'true') || {}).textContent?.trim() || '',
  };
});

// 命中测试：菜单顶部/底部中心点必须真正命中菜单本身，且必须落在视口内。
// 注意排除 diagnosis clippers 对 body 的误报：这里只报告「真正包住菜单」的祖先，
// 即 overflow 非 visible 或自身可滚动，且其可视盒确实小于菜单盒。
const menuHitTest = (page, atTop) => page.evaluate((top) => {
  const menu = document.querySelector('#updateVersionMenu');
  if (!menu) return { ok: false, reason: 'menu missing' };
  const rect = menu.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  const y = top ? rect.top + 6 : rect.bottom - 6;
  const inViewport = y >= 0 && y <= window.innerHeight;
  // 视口外不做 elementFromPoint（必然命中 null），直接判失败并给出坐标。
  const hit = inViewport ? document.elementFromPoint(x, y) : null;
  const ok = inViewport && Boolean(hit) && (hit === menu || menu.contains(hit));
  const clippers = [];
  for (let node = menu.parentElement; node && node !== document.documentElement; node = node.parentElement) {
    const style = getComputedStyle(node);
    const box = node.getBoundingClientRect();
    const overflowClipped = style.overflowX !== 'visible' || style.overflowY !== 'visible';
    if (`${node.tagName}`.toLowerCase() === 'body' || `${node.tagName}`.toLowerCase() === 'html') continue;
    // 祖先容得下整个菜单就不是裁剪者。
    const fits = box.top <= rect.top + 1 && box.bottom >= rect.bottom - 1;
    if (overflowClipped && !fits) {
      clippers.push(`${node.tagName.toLowerCase()}#${node.id || '-'}[${style.overflowX}/${style.overflowY}]`);
    }
  }
  return {
    ok,
    inViewport,
    y: Math.round(y),
    menuTop: Math.round(rect.top),
    menuBottom: Math.round(rect.bottom),
    viewport: window.innerHeight,
    hit: hit ? `${hit.tagName.toLowerCase()}#${hit.id || '-'}` : 'null',
    clippers: clippers.slice(0, 5),
  };
}, atTop);

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });

  let phase = 'available';
  const requests = [];
  await page.route('**/api/update**', async (route) => {
    const { pathname } = new URL(route.request().url());
    requests.push(`${route.request().method()} ${pathname} -> phase=${phase}`);
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(statusPayload(phase)),
    });
  });

  // 失败诊断：把面板可见状态与 toast 文案一并带出，避免只看到「断言不成立」。
  const diagnose = async () => page.evaluate(() => ({
    latest: (document.querySelector('#latestVersion')?.textContent || '').trim(),
    current: (document.querySelector('#currentVersion')?.textContent || '').trim(),
    label: (document.querySelector('#updateVersionLabel')?.textContent || '').trim(),
    selectValue: document.querySelector('#updateVersionSelect')?.value || '',
    toasts: [...document.querySelectorAll('.toast')].map((n) => n.textContent.trim()).filter(Boolean),
  }));

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });

    // 设置 → 软件更新（切面板时会拉 /api/update，渲染注入的版本表）。
    await page.click('#openSettings');
    await page.click('[data-settings-tab="updates"]');
    await page.waitForSelector('[data-settings-panel="updates"]:not([hidden])', { timeout: 10000 });
    await page.waitForFunction(
      () => !document.querySelector('#updateVersionTrigger')?.disabled,
      null, { timeout: 10000 },
    );

    // ---------- A. 列表内容 ----------
    let snap = await snapshot(page);
    check('combobox 已显示', snap.wrapHidden === false, `wrapHidden=${snap.wrapHidden}`);
    check('触发按钮可用', snap.triggerDisabled === false, `disabled=${snap.triggerDisabled}`);
    check(
      `下拉选项数=${TOTAL_VERSIONS}（过滤 2.0.0、不截断为 20 条）`,
      snap.optionCount === TOTAL_VERSIONS,
      `实际 ${snap.optionCount}`,
    );
    check('2.0.0 已被过滤', !snap.optionTexts.some((t) => t.startsWith('2.0.0')), snap.optionTexts.join(','));
    check(
      '当前版本（列表最末）仍在列表中',
      snap.optionTexts.some((t) => t.startsWith(`${CURRENT_VERSION}（当前）`)),
      snap.optionTexts.slice(-1)[0],
    );
    check('原生 select 同步全部可安装值', snap.nativeValues.length === TOTAL_VERSIONS, `原生 ${snap.nativeValues.length}`);

    // ---------- D1. 默认聚焦最新可更新版本 ----------
    check(
      `渲染后默认聚焦最新（2.1.${LATEST_PATCH}）`,
      snap.triggerText.startsWith(`2.1.${LATEST_PATCH}`),
      `label=${snap.triggerText}`,
    );
    check('默认不选中当前版本', !snap.triggerText.includes('（当前）'), `label=${snap.triggerText}`);

    // ---------- B. 展开：固定 20 行 + 可滚动 + 不被裁剪 ----------
    await page.click('#updateVersionTrigger');
    await page.waitForSelector('#updateVersionMenu:not([hidden])', { timeout: 5000 });
    snap = await snapshot(page);
    check('菜单已展开', snap.menuHidden === false);
    // 高度契约：上限 20 行；视口装不下时按可用空间收窄（此时仍必须完全在视口内，
    // 由下面的命中测试把关）。所以断言「不超过 20 行」+「不超出视口」。
    check(
      `展开高度不超过 20 行（${MENU_BOX_HEIGHT}px），实际 ${snap.menuBoxHeight}px`,
      snap.menuBoxHeight > 0 && snap.menuBoxHeight <= MENU_BOX_HEIGHT,
      `boxHeight=${snap.menuBoxHeight} maxHeight=${snap.menuMaxHeight}`,
    );
    check(
      '菜单完全落在视口内',
      snap.menuTop >= 0 && snap.menuBottom <= snap.viewport,
      `top=${snap.menuTop} bottom=${snap.menuBottom} viewport=${snap.viewport}`,
    );
    const capacity = Math.floor((snap.menuClientHeight - 6) / ROW_MIN_PX);
    check(
      `可见行数已达上限或受视口限制（实测 ${capacity} 行）`,
      capacity >= 1 && capacity <= 21,
      `clientHeight=${snap.menuClientHeight} maxHeight=${snap.menuMaxHeight}`,
    );
    check(
      '内容超出可滚动',
      snap.menuScrollHeight > snap.menuClientHeight,
      `scrollHeight=${snap.menuScrollHeight} clientHeight=${snap.menuClientHeight}`,
    );

    const hitTop = await menuHitTest(page, true);
    check('菜单顶部可见（未被祖先裁剪）', hitTop.ok,
      `命中 ${hitTop.hit} @y=${hitTop.y} 视口=${hitTop.viewport} 裁剪祖先=${hitTop.clippers.join(',') || '无'}`);
    const hitBottom = await menuHitTest(page, false);
    check('菜单底部可见（未被祖先裁剪）', hitBottom.ok,
      `命中 ${hitBottom.hit} @y=${hitBottom.y} 菜单底=${hitBottom.menuBottom} 视口=${hitBottom.viewport}`
      + ` 裁剪祖先=${hitBottom.clippers.join(',') || '无'}`);

    await page.evaluate(() => { document.querySelector('#updateVersionMenu').scrollTop = 1e6; });
    await page.waitForTimeout(150);
    const lastHit = await menuHitTest(page, false);
    check('滚动到底后最后一项可见', lastHit.ok, `命中 ${lastHit.hit} @y=${lastHit.y}`);

    // ---------- C. 键盘 ----------
    // 用 Escape（开→关）而非 Enter 收尾：Enter 在展开态是「收起」，会让下面的
    // 首按 ArrowDown 变成「展开而不移动选中」，把键盘断言引向错误结论。
    await page.focus('#updateVersionTrigger');
    await page.keyboard.press('Escape');
    await page.waitForTimeout(100);
    check('Escape 关闭菜单', (await snapshot(page)).menuHidden === true);

    const beforeKey = (await snapshot(page)).triggerText;
    await page.keyboard.press('ArrowDown');
    await page.waitForTimeout(150);
    snap = await snapshot(page);
    check('ArrowDown 展开菜单', snap.menuHidden === false);
    // 最新版本已是第一个选项，所以 ArrowDown 会回绕到最末（2.1.1），标签必须变化。
    check('ArrowDown 移动选中项', snap.triggerText !== beforeKey, `before=${beforeKey} after=${snap.triggerText}`);
    check('高亮项与标签一致', snap.selectedText === snap.triggerText, `sel=${snap.selectedText} label=${snap.triggerText}`);

    await page.keyboard.press('ArrowUp');
    await page.waitForTimeout(150);
    check('ArrowUp 回到上一项', (await snapshot(page)).triggerText === beforeKey);

    await page.keyboard.press('Escape');
    await page.waitForTimeout(120);
    check('Escape 收起菜单', (await snapshot(page)).menuHidden === true);

    // 空格开关键：展开态下再按一次应收起。
    await page.keyboard.press('Space');
    await page.waitForTimeout(120);
    check('Space 展开菜单', (await snapshot(page)).menuHidden === false);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(120);
    check('Enter 收起菜单', (await snapshot(page)).menuHidden === true);

    // 鼠标选择历史版本：标签与原生 select 同步。
    await page.click('#updateVersionTrigger');
    await page.waitForSelector('#updateVersionMenu:not([hidden])', { timeout: 5000 });
    const target = 'v2.1.1';
    await page.click(`.update-version-option[data-value="${target}"]`);
    await page.waitForTimeout(200);
    snap = await snapshot(page);
    check('鼠标点击后菜单关闭', snap.menuHidden === true);
    check('点击项写入原生 select', snap.nativeValue === target, `native=${snap.nativeValue}`);
    check('点击项同步到触发标签', snap.triggerText.startsWith('2.1.1'), `label=${snap.triggerText}`);

    // ---------- D2. 已是最新时聚焦当前版本 ----------
    // 必须真的点「检查更新」：updateAutoSelectLatest 只在 checkUpdate() 里置位，
    // 单纯切面板重渲染不会走默认聚焦分支。
    // 该 payload 刻意仍列出比当前版本更新的条目：这样「聚焦当前版本」才可能与
    // 「退化成选中第一项 / 保留此前手选值」区分开（后者是本用例要防的回归）。
    phase = 'current';
    // 记录每一次渲染：MutationObserver 抓 select 重建与 label 变化，
    // 40ms 轮询会漏掉「先清空再重建」这类瞬时中间态。
    await page.evaluate(() => {
      window.__renderTrace = [];
      const record = (tag) => window.__renderTrace.push({
        tag,
        label: (document.querySelector('#updateVersionLabel')?.textContent || '').trim().slice(0, 20),
        select: document.querySelector('#updateVersionSelect')?.value ?? '?',
        options: document.querySelector('#updateVersionSelect')?.options.length ?? -1,
        latest: (document.querySelector('#latestVersion')?.textContent || '').trim().slice(0, 16),
      });
      record('start');
      const select = document.querySelector('#updateVersionSelect');
      new MutationObserver(() => record('select'))
        .observe(select, { childList: true, attributes: true, attributeFilter: ['disabled'] });
      new MutationObserver(() => record('latest'))
        .observe(document.querySelector('#latestVersion'), { childList: true, characterData: true, subtree: true });
    });
    const samples = [];
    try {
      await page.click('#checkUpdate');
      await page.waitForFunction(
        () => (document.querySelector('#updateVersionLabel')?.textContent || '').includes('（当前）'),
        null, { timeout: 10000 },
      ).catch(() => {});
      await page.waitForTimeout(400);
      samples.push(...await page.evaluate(() => window.__renderTrace.map((s) => ({
        label: `${s.tag}:${s.label}`,
        select: `${s.select}(${s.options})`,
      }))));
    } finally {
      /* 无需清理：trace 挂在页面上下文，随用例结束销毁 */
    }
    const latestSnap = await snapshot(page);
    const d2 = await diagnose();
    check(
      '已是最新时聚焦当前版本',
      latestSnap.triggerText.includes('（当前）'),
      `label=${latestSnap.triggerText} 最新行=${d2.latest} select=${d2.selectValue}`
      + ` toast=${d2.toasts.join('/') || '无'} 请求=${requests.slice(-4).join(' | ')}`
      + ` 采样=${samples.map((s) => `${s.label}/${s.select || '-'}`).join(' → ')}`,
    );

    check('零页面错误', pageErrors.length === 0, pageErrors.join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, error.message);
  } finally {
    await browser.close();
  }

  console.log('');
  if (failures.length) {
    console.log(`❌ 失败 ${failures.length} 项：`);
    failures.forEach((name) => console.log('  - ' + name));
    process.exit(1);
  }
  console.log('✅ 版本下拉回归通过');
  process.exit(0);
})();
