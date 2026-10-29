// 软件更新「版本列表」两栏回归（源码 server + 拦截 /api/update* 注入固定版本表）。
//
// 覆盖（3.9.7 把「目标版本下拉」换成左栏列表）：
//   A. 只渲染可自动更新的版本（2.0.0 及更早被过滤），历史版本全量保留、不被截断；
//   B. 左栏列表有固定高度并可滚动，首尾项都能被真实点到（elementFromPoint 命中测试）；
//   C. 点一条即换右栏说明，选中值同步回原生 #updateVersionSelect（installUpdate 的口径）；
//   D. 筛选框按版本号过滤，计数随之变化；
//   E. 检查到新版本时默认选中最新、已是最新时选中当前版本；
//   F. ≤760px 收成上下两栏（列表限高 180px），说明接在列表下方；
//   G. 来源条如实显示列表来源；全程零 pageerror / console.error。
//
// 运行：$env:NODE_PATH="<node_modules>"; node verify\update_version_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8798';
const LATEST_PATCH = 26;            // 2.1.26 是最新
const CURRENT_VERSION = '2.1.0';    // 当前版本必须在列表里，否则「选中当前版本」无从谈起
const TOTAL_VERSIONS = LATEST_PATCH + 1;  // 2.1.26 … 2.1.0
// 与 styles.css 的 `.update-version-list { max-height: 296px }` 对齐。
const DESKTOP_LIST_MAX = 296;
const NARROW_LIST_MAX = 180;

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
      release_url: `https://github.com/yc883123/cat-chat/releases/tag/v${version}`,
      release_notes: [`${version} 的更新说明`, `${version} 条目一`, `${version} 条目二`],
      installable: true,
      current: version === CURRENT_VERSION,
    });
  }
  releases.push({
    tag: 'v2.0.0', version: '2.0.0', published_at: '2025-01-01T00:00:00Z',
    release_url: 'https://github.com/yc883123/cat-chat/releases/tag/v2.0.0',
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
    releases_meta: {
      source: 'api',
      fetched_at: 1757800000,
      legacy_cache: false,
      truncated: false,
    },
    checked_at: 1757800000,
  };
}

const snapshot = (page) => page.evaluate(() => {
  const browserEl = document.querySelector('#updateBrowser');
  const list = document.querySelector('#updateVersionList');
  const native = document.querySelector('#updateVersionSelect');
  const items = list ? [...list.querySelectorAll('.update-version-item')] : [];
  const rect = (el) => (el ? el.getBoundingClientRect() : null);
  const style = (el) => (el ? getComputedStyle(el) : null);
  return {
    browserHidden: browserEl ? browserEl.hidden : null,
    browserColumns: style(browserEl) ? style(browserEl).gridTemplateColumns.split(' ').length : 0,
    listClientHeight: list ? list.clientHeight : 0,
    listScrollHeight: list ? list.scrollHeight : 0,
    listMaxHeight: style(list) ? style(list).maxHeight : '',
    itemCount: items.length,
    itemTexts: items.map((node) => (node.querySelector('.update-version-name')?.textContent || '').trim()),
    selectedText: (items.find((node) => node.getAttribute('aria-selected') === 'true')
      ?.querySelector('.update-version-name')?.textContent || '').trim(),
    currentBadges: items
      .filter((node) => node.querySelector('.update-version-badge.is-current'))
      .map((node) => (node.querySelector('.update-version-name')?.textContent || '').trim()),
    countText: (document.querySelector('#updateVersionCount')?.textContent || '').trim(),
    sourceText: (document.querySelector('#updateSourceText')?.textContent || '').trim(),
    nativeValue: native ? native.value : '',
    nativeValues: native ? [...native.options].map((option) => option.value) : [],
    notesVersion: (document.querySelector('#updateNotesVersion')?.textContent || '').trim(),
    notesTitle: (document.querySelector('.update-notes-title')?.textContent || '').trim(),
    notesBody: (document.querySelector('#updateNotesBody')?.textContent || '').trim(),
    notesTop: Math.round(rect(document.querySelector('#updateNotesBody'))?.top || 0),
    listBottom: Math.round(rect(list)?.bottom || 0),
    installText: (document.querySelector('#installUpdate')?.textContent || '').trim(),
    installHidden: document.querySelector('#installUpdate')?.hidden ?? null,
  };
});

// 命中测试：首/末项中心点必须真正命中该项本身（只看 getBoundingClientRect 对裁剪无感）。
const itemHitTest = (page, selector) => page.evaluate((sel) => {
  const list = document.querySelector('#updateVersionList');
  const item = list ? list.querySelector(sel) : null;
  if (!list || !item) return { ok: false, reason: 'missing item' };
  const rect = item.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const inViewport = y >= 0 && y <= window.innerHeight;
  const hit = inViewport ? document.elementFromPoint(x, y) : null;
  return {
    ok: inViewport && Boolean(hit) && (hit === item || item.contains(hit)) && rect.height >= 24,
    y: Math.round(y),
    height: Math.round(rect.height),
    hit: hit ? `${hit.tagName.toLowerCase()}#${hit.id || '-'}` : 'null',
  };
}, selector);

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    // 资源加载失败由 response 监听单独统计（下面会排除隔离现场造成的 /api/file 403），
    // 这里只收应用自身打印的 console.error，避免同一件事报两次。
    if (/Failed to load resource/.test(msg.text())) return;
    pageErrors.push(`console.error: ${msg.text()}`);
  });
  page.on('response', (response) => {
    const url = response.url();
    // 本冒烟把 data_dir 指到临时目录：真实配置里的背景图在那边没有访问口令，必然 403。
    // 这是隔离现场造成的，不是本次改动的问题，排除 /api/file。
    if (response.status() >= 400 && !url.includes('/api/file')) {
      pageErrors.push(`HTTP ${response.status()} ${url}`);
    }
  });

  let phase = 'available';
  const requests = [];
  await page.route('**/api/update**', async (route) => {
    const { pathname, searchParams } = new URL(route.request().url());
    requests.push(`${route.request().method()} ${pathname}${searchParams.get('tag') ? `?tag=${searchParams.get('tag')}` : ''}`);
    if (pathname === '/api/update/notes') {
      const tag = searchParams.get('tag') || '';
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          tag,
          version: tag.replace(/^v/, ''),
          release_notes: [`${tag} 按需取回的说明`, '按需条目'],
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(statusPayload(phase)),
    });
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });

    // 设置 → 软件更新（切面板时会拉 /api/update，渲染注入的版本表）。
    await page.click('#openSettings');
    await page.click('[data-settings-tab="updates"]');
    await page.waitForSelector('[data-settings-panel="updates"]:not([hidden])', { timeout: 10000 });
    await page.waitForSelector('#updateBrowser:not([hidden])', { timeout: 10000 });

    // ---------- A. 列表内容 ----------
    let snap = await snapshot(page);
    check('两栏版本浏览已显示', snap.browserHidden === false, `hidden=${snap.browserHidden}`);
    check(
      `版本项数=${TOTAL_VERSIONS}（过滤 2.0.0、不截断）`,
      snap.itemCount === TOTAL_VERSIONS,
      `实际 ${snap.itemCount}`,
    );
    check('2.0.0 已被过滤', !snap.itemTexts.includes('2.0.0'), snap.itemTexts.slice(-3).join(','));
    check(
      `当前版本 ${CURRENT_VERSION} 仍在列表且带「当前」徽标`,
      snap.currentBadges.join(',') === CURRENT_VERSION,
      `徽标=${snap.currentBadges.join(',') || '无'}`,
    );
    check('原生 select 同步全部可安装值', snap.nativeValues.length === TOTAL_VERSIONS, `原生 ${snap.nativeValues.length}`);
    check('来源条如实写出列表来源', snap.sourceText.includes('版本列表来自 GitHub'), snap.sourceText);
    check('列表计数文案正确', snap.countText.includes(String(TOTAL_VERSIONS)), snap.countText);

    // ---------- D1. 默认选中最新版本 ----------
    check(`默认选中最新（2.1.${LATEST_PATCH}）`, snap.selectedText === `2.1.${LATEST_PATCH}`, `选中=${snap.selectedText}`);
    check('右栏显示所选版本', snap.notesVersion === `2.1.${LATEST_PATCH}`, snap.notesVersion);
    check('右栏渲染首行标题与条目', snap.notesBody.includes('条目一') && snap.notesBody.includes('条目二'), snap.notesBody.slice(0, 60));
    check('桌面两栏布局', snap.browserColumns === 2, `列数=${snap.browserColumns}`);

    // ---------- B. 高度契约 + 可滚动 + 不被裁剪 ----------
    check(
      `列表限高 ${DESKTOP_LIST_MAX}px（实际 maxHeight=${snap.listMaxHeight}）`,
      snap.listMaxHeight === `${DESKTOP_LIST_MAX}px`,
      snap.listMaxHeight,
    );
    check(
      '内容超出可滚动',
      snap.listScrollHeight > snap.listClientHeight,
      `scrollHeight=${snap.listScrollHeight} clientHeight=${snap.listClientHeight}`,
    );
    const firstHit = await itemHitTest(page, '.update-version-item:first-child');
    check('首项可见且可点到（未被祖先裁剪）', firstHit.ok,
      `命中 ${firstHit.hit} @y=${firstHit.y} 高=${firstHit.height}`);
    await page.evaluate(() => {
      const list = document.querySelector('#updateVersionList');
      // 先把列表滚进设置面板的可视区（面板自身可滚动），再滚到列表底。
      list.scrollIntoView({ block: 'center' });
      list.scrollTop = 1e6;
    });
    await page.waitForTimeout(150);
    const lastHit = await itemHitTest(page, '.update-version-item:last-child');
    check('滚到底后末项可见且可点到', lastHit.ok,
      `命中 ${lastHit.hit} @y=${lastHit.y} 高=${lastHit.height}`);

    // ---------- C. 点选联动 ----------
    await page.click('.update-version-item[data-value="v2.1.1"]');
    await page.waitForTimeout(200);
    snap = await snapshot(page);
    check('点击项写入原生 select', snap.nativeValue === 'v2.1.1', `native=${snap.nativeValue}`);
    check('右栏切到所点版本', snap.notesVersion === '2.1.1', `notes=${snap.notesVersion}`);
    check('高亮跟随选中项', snap.selectedText === '2.1.1', `选中=${snap.selectedText}`);
    check('「立即更新」文案带上目标版本', snap.installText.includes('2.1.1'), snap.installText);
    check('「立即更新」可见', snap.installHidden === false, `hidden=${snap.installHidden}`);

    // ---------- D. 筛选 ----------
    await page.fill('#updateVersionFilter', '2.1.2');
    await page.waitForTimeout(200);
    snap = await snapshot(page);
    check(
      '筛选后只剩匹配项（2.1.2 / 2.1.20-26）',
      snap.itemCount === 8 && snap.itemTexts.every((text) => text.includes('2.1.2')),
      `count=${snap.itemCount} ${snap.itemTexts.slice(0, 4).join(',')}`,
    );
    check('筛选时计数显示匹配数', snap.countText.includes('匹配'), snap.countText);
    await page.fill('#updateVersionFilter', '');
    await page.waitForTimeout(200);
    check('清空筛选后恢复全量', (await snapshot(page)).itemCount === TOTAL_VERSIONS);

    // ---------- E. 已是最新时选中当前版本 ----------
    phase = 'current';
    await page.click('#checkUpdate');
    await page.waitForFunction(
      () => (document.querySelector('.update-version-item[aria-selected="true"] .update-version-name')
        ?.textContent || '').trim() === '2.1.0',
      null, { timeout: 10000 },
    ).catch(() => {});
    snap = await snapshot(page);
    check('已是最新时选中当前版本', snap.selectedText === CURRENT_VERSION,
      `选中=${snap.selectedText} 请求=${requests.slice(-3).join(' | ')}`);

    // ---------- F. 窄屏上下两栏 ----------
    await page.setViewportSize({ width: 720, height: 900 });
    await page.waitForTimeout(250);
    snap = await snapshot(page);
    check('窄屏收成一列', snap.browserColumns === 1, `列数=${snap.browserColumns}`);
    check(
      `窄屏列表限高 ${NARROW_LIST_MAX}px`,
      snap.listMaxHeight === `${NARROW_LIST_MAX}px`,
      snap.listMaxHeight,
    );
    check('窄屏说明接在列表下方', snap.notesTop >= snap.listBottom - 1,
      `notesTop=${snap.notesTop} listBottom=${snap.listBottom}`);

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
  console.log('✅ 版本列表回归通过');
  process.exit(0);
})();
