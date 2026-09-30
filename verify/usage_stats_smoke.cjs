// 用量统计「分析视图」冒烟（隔离源码实例，脚本自起自停；端口 8812）。
// 覆盖：端点 bucket 契约（by_bucket 桶×模型行）/ KPI 六卡 / 手绘 SVG 五图 /
//       明细表与维度切换 / 图例隐藏 / 筛选弹窗（粒度切换）/ 偏好弹窗落库
//       settings.usage_dash / 供应商弹层三档单价回填回归 /
//       ★ 手机端弹层可视视口适配（软键盘桩：dialog max-height 压进可视区）。
// 运行：node verify/usage_stats_smoke.cjs（playwright 从仓库 node_modules 或 NODE_PATH 解析）。
const { spawn } = require('child_process');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8812';
const PORT = Number(new URL(BASE).port);
const PYTHON = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(pathname, options = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(`${BASE}${pathname}`, init);
  return response.json().catch(() => ({}));
}

async function waitForHealth(deadlineMs = 30000) {
  const deadline = Date.now() + deadlineMs;
  while (Date.now() < deadline) {
    try {
      const payload = await apiJson('/api/health');
      if (payload.status === 'ok') return true;
    } catch (_) { /* not ready yet */ }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return false;
}

async function openUsageTab(page) {
  await page.evaluate(() => document.querySelector('button[data-settings-tab="usage"]').click());
  // 打开页签 → loadUsageStats 拉数据 → 渲染完成以明细表有行为准（1 天窗口至少
  // smoke-ds 与孤儿两行）。
  await page.waitForFunction(() => {
    const panel = document.querySelector('[data-settings-panel="usage"]');
    return panel && !panel.hidden && document.querySelector('#usageDetailBody')?.children.length;
  }, undefined, { timeout: 8000 });
}

async function main() {
  const server = spawn(PYTHON, [path.join(__dirname, '_usage_stats_serve.py')], {
    cwd: ROOT,
    env: { ...process.env, NAIBA_TMP_PORT: String(PORT) },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let serverLog = '';
  let stopped = false;
  server.stdout.on('data', (chunk) => { serverLog += chunk; });
  server.stderr.on('data', (chunk) => { serverLog += chunk; });
  server.on('exit', (code) => {
    if (code !== null && code !== 0 && !stopped) console.log(`[serve exited early code=${code}]\n${serverLog}`);
  });
  const stopServer = () => {
    if (stopped) return;
    stopped = true;
    try { server.kill(); } catch (_) { /* already gone */ }
  };
  process.on('exit', stopServer);

  try {
    if (!(await waitForHealth())) throw new Error(`隔离实例未就绪：\n${serverLog}`);

    /* ── 端点契约：bucket 参数 ── */
    const statsHour = await apiJson('/api/usage/stats?days=30&bucket=hour');
    check('端点：bucket=hour 时响应带 bucket=hour 且旧键齐全',
      statsHour.bucket === 'hour'
      && ['totals', 'today', 'by_model', 'by_provider', 'by_day', 'by_bucket', 'first_record_at'].every((key) => key in statsHour));
    check('端点：by_bucket 是「桶×模型」行（键形如 HH:00、带 cost/priced/provider_name）',
      statsHour.by_bucket.length >= 3
      && statsHour.by_bucket.every((row) => /^\d{4}-\d{2}-\d{2} \d{2}:00$/.test(row.day) && 'cost' in row && 'priced' in row && 'provider_name' in row),
      JSON.stringify(statsHour.by_bucket.slice(0, 2)));
    const statsDay = await apiJson('/api/usage/stats?days=30');
    // day 模式下两者键同为天级但**行数不同**：by_bucket 是「天×模型」原始行
    // （day-0 有 smoke-ds 与 smoke-orphan 两个模型 ⇒ 3 行），by_day 是计价后
    // 折叠的天级单行（⇒ 2 行）。折叠是 app 层职责，行数相等不是契约。
    check('端点：默认 bucket=day，by_bucket 为天×模型原始行、by_day 为折叠天级行',
      statsDay.bucket === 'day'
      && statsDay.by_bucket.length === 3 && statsDay.by_day.length === 2
      && statsDay.by_bucket.every((row) => /^\d{4}-\d{2}-\d{2}$/.test(row.day) && 'cost' in row && 'priced' in row && 'provider_name' in row)
      && statsDay.by_day.every((row) => /^\d{4}-\d{2}-\d{2}$/.test(row.day) && Array.isArray(row.costs)),
      JSON.stringify({ bucketRows: statsDay.by_bucket.length, dayRows: statsDay.by_day.length }));
    const badBucket = await apiJson('/api/usage/stats?bucket=week');
    check('端点：非法 bucket 显式 400', badBucket.error === 'bucket 必须是 day 或 hour', JSON.stringify(badBucket));

    const browser = await chromium.launch({ channel: 'msedge', headless: true });

    /* ══ 桌面视图：分析视图渲染与交互 ══ */
    const desktop = await browser.newPage({ viewport: { width: 1440, height: 960 } });
    await desktop.goto(BASE, { waitUntil: 'domcontentloaded' });
    // 隔离实例首次启动会异步弹「新手引导」（无可用供应商 + 未跳过 ⇒ maybeShowOnboarding
    // showModal）。它稍后盖到 top layer 之上，真鼠标交互（tooltip hover）会被整条
    // mouseleave 链打断、elementFromPoint 也指向引导弹窗。先写「已跳过」标记再重载
    // ——与真人点「跳过」落的是同一个 localStorage 键，杜绝会话中途弹窗。
    await desktop.evaluate(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
    await desktop.reload({ waitUntil: 'domcontentloaded' });
    await desktop.waitForFunction(() => Boolean(window.__naibaReady || document.querySelector('#openSettings')), undefined, { timeout: 15000 });
    await desktop.evaluate(() => document.querySelector('#openSettings').click());
    await openUsageTab(desktop);

    const kpi = await desktop.evaluate(() => ({
      calls: document.querySelector('#usageKpiCalls')?.textContent.trim() || '',
      cost: document.querySelector('#usageKpiCost')?.textContent.trim() || '',
      tokens: document.querySelector('#usageKpiTokens')?.textContent.trim() || '',
      rpm: document.querySelector('#usageKpiRpm')?.textContent.trim() || '',
      tpm: document.querySelector('#usageKpiTpm')?.textContent.trim() || '',
      cache: document.querySelector('#usageKpiCache')?.textContent.trim() || '',
    }));
    check('KPI：总调用 4 次（run-1 的 3 + run-3 的 1）', kpi.calls === '4次' || kpi.calls === '4 次', JSON.stringify(kpi));
    check('KPI：总耗费含 ¥（1 天窗口已定价 ¥2）', kpi.cost.startsWith('¥'), kpi.cost);
    check('KPI：总 TOKEN 与缓存命中率有值', /tok/.test(kpi.tokens) && /%/.test(kpi.cache), JSON.stringify(kpi));

    const charts = await desktop.evaluate(() => ({
      dist: Boolean(document.querySelector('#usageDistBox svg')),
      trend: Boolean(document.querySelector('#usageTrendBox svg')),
      prov: Boolean(document.querySelector('#usageProvBox svg')),
      cache: Boolean(document.querySelector('#usageCacheBox svg')),
      sankey: Boolean(document.querySelector('#usageSankeyBox svg')),
      distLegend: [...document.querySelectorAll('#usageDistLegend .usage-legend-item')].map((el) => el.textContent.trim()),
      distSum: document.querySelector('#usageDistSum')?.textContent.trim() || '',
    }));
    check('图表：五个手绘 SVG 全部渲染', charts.dist && charts.trend && charts.prov && charts.cache && charts.sankey, JSON.stringify(charts));
    check('图表：分布图图例含 smoke-ds 系列', charts.distLegend.some((name) => name.includes('smoke-ds')), JSON.stringify(charts.distLegend));
    check('图表：分布图合计注明 $ 折算口径', charts.distSum.includes('折算'), charts.distSum);

    // 明细表：1 天窗口 smoke-ds 费用 = 0.5M×2 + 0.5M×0.4 + 0.1M×8 = ¥2。
    const detail = await desktop.evaluate(() => ({
      title: document.querySelector('#usageDetailTitle')?.textContent.trim() || '',
      rows: [...document.querySelectorAll('#usageDetailBody tr')].map((row) =>
        [...row.querySelectorAll('td')].map((cell) => cell.textContent.trim())),
    }));
    check('明细表：默认「按模型明细」且 smoke-ds 行费用 ¥2',
      detail.title === '按模型明细' && detail.rows.some((row) => row[0] === 'smoke-ds' && row[7].startsWith('¥2')),
      JSON.stringify(detail));
    check('明细表：孤儿模型行显示「未定价」占位', detail.rows.some((row) => row[0].includes('smoke-orphan') && row[7] === '未定价'), JSON.stringify(detail));

    // 切 29 天：窗口含两天前的 run-2 → smoke-ds 费用变 ¥2.124（0.54M×2+0.51M×0.4+0.105M×8）。
    await desktop.evaluate(() => document.querySelector('[data-usage-range="29"]').click());
    await desktop.waitForFunction(() => {
      const rows = [...document.querySelectorAll('#usageDetailBody tr')];
      const ds = rows.find((tr) => tr.cells[0]?.textContent.trim() === 'smoke-ds');
      return ds && ds.cells[7]?.textContent.includes('2.124');
    }, undefined, { timeout: 8000 });
    check('窗口切换：29 天 chip 重新请求，smoke-ds 费用变 ¥2.124（run-2 入窗）', true);

    // 维度切换：明细表标题变、供应商行出现（纯前端重渲染，不发请求）。
    await desktop.evaluate(() => document.querySelector('[data-usage-dim="provider"]').click());
    const providerView = await desktop.evaluate(() => ({
      title: document.querySelector('#usageDetailTitle')?.textContent.trim() || '',
      dimActive: document.querySelector('[data-usage-dim="provider"]')?.classList.contains('active'),
      rows: [...document.querySelectorAll('#usageDetailBody tr')].map((row) => row.cells[0]?.textContent.trim()),
    }));
    check('维度切换：供应商视图标题与行（冒烟 DeepSeek / 已删除的 API）',
      providerView.title === '按 API 供应商明细' && providerView.dimActive === true
      && providerView.rows.includes('冒烟 DeepSeek') && providerView.rows.includes('已删除的 API'),
      JSON.stringify(providerView));
    await desktop.evaluate(() => document.querySelector('[data-usage-dim="model"]').click());

    // 图例隐藏：点 smoke-ds → 分布图重绘（hidden 集合生效）。
    const beforeHide = await desktop.evaluate(() => document.querySelectorAll('#usageDistBox svg rect').length);
    await desktop.evaluate(() => {
      [...document.querySelectorAll('#usageDistLegend .usage-legend-item')]
        .find((el) => el.textContent.includes('smoke-ds')).click();
    });
    const afterHide = await desktop.evaluate(() => ({
      rects: document.querySelectorAll('#usageDistBox svg rect').length,
      off: [...document.querySelectorAll('#usageDistLegend .usage-legend-item')]
        .find((el) => el.textContent.includes('smoke-ds'))?.classList.contains('off'),
    }));
    check('图例：点击隐藏系列 → 图例 off 态 + 柱形减少',
      afterHide.off === true && afterHide.rects < beforeHide, JSON.stringify({ beforeHide, afterHide }));
    await desktop.evaluate(() => {
      [...document.querySelectorAll('#usageDistLegend .usage-legend-item')]
        .find((el) => el.textContent.includes('smoke-ds')).click();
    });

    // tooltip：hover 分布图 hit 区（每桶一个透明矩形，svg 最上层）出浮层（挂在
    // #settingsDialog 内、top layer 可见）。前序步骤把窗口切到了 29 天而粒度仍是
    // hour ⇒ 696 个 1px 宽的桶、hit 区窄到没法 hover（曾因此 3 次尝试全脱靶）——
    // 先把粒度切回「天」（29 桶 × ~24px），并等 hit 区宽度达标再动手。
    await desktop.evaluate(() => {
      const gran = document.querySelector('#usageGranSel');
      gran.value = 'day';
      gran.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await desktop.waitForFunction(() => {
      const rects = [...document.querySelectorAll('#usageDistBox svg rect')]
        .filter((r) => r.getAttribute('fill') === 'transparent');
      return rects.some((r) => r.getBoundingClientRect().width > 4);
    }, undefined, { timeout: 8000 });
    await desktop.evaluate(() => document.querySelector('#usageDistBox').scrollIntoView({ block: 'center' }));
    // 失败诊断：捕获级事件日志 + 打开的 dialog 清单（只在本段埋，check 前若失败打印）。
    await desktop.evaluate(() => {
      window.__evts = [];
      for (const type of ['mouseover', 'mouseout', 'mouseenter', 'mouseleave', 'mousemove']) {
        document.addEventListener(type, (e) => {
          const t = e.target;
          const fill = t.getAttribute && t.getAttribute('fill');
          const tag = `${t.tagName}${t.id ? `#${t.id}` : ''}${fill ? `[fill=${fill}]` : (t.getAttribute && t.getAttribute('class') ? `.${t.getAttribute('class')}` : '')}`;
          window.__evts.push(`${type}@${Math.round(e.clientX)},${Math.round(e.clientY)}→${tag}`);
        }, true);
      }
    });
    let tooltipShown = false;
    for (let attempt = 0; attempt < 3 && !tooltipShown; attempt += 1) {
      const hitPoint = await desktop.evaluate(() => {
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const hits = [...document.querySelectorAll('#usageDistBox svg rect')]
          .filter((r) => r.getAttribute('fill') === 'transparent');
        for (const r of hits) {
          const b = r.getBoundingClientRect();
          const ix = Math.max(b.x, 0);
          const iy = Math.max(b.y, 0);
          const ix2 = Math.min(b.x + b.width, vw);
          const iy2 = Math.min(b.y + b.height, vh);
          if (ix2 - ix > 4 && iy2 - iy > 4) return { x: (ix + ix2) / 2, y: (iy + iy2) / 2 };
        }
        return null;
      });
      if (!hitPoint) {
        await desktop.evaluate(() => document.querySelector('#usageDistBox').scrollIntoView({ block: 'center' }));
        continue;
      }
      // 两步 move（先进入再微动）保证目标点至少派发一次 mousemove。
      await desktop.mouse.move(hitPoint.x - 20, hitPoint.y);
      await desktop.mouse.move(hitPoint.x, hitPoint.y);
      await desktop.waitForFunction(() => {
        const tip = document.querySelector('#settingsDialog .usage-tooltip');
        return tip && tip.style.display === 'block';
      }, undefined, { timeout: 2000 }).catch(() => {});
      tooltipShown = await desktop.evaluate(() => {
        const tip = document.querySelector('#settingsDialog .usage-tooltip');
        return Boolean(tip && tip.style.display === 'block' && tip.textContent.length > 0);
      });
    }
    if (!tooltipShown) {
      console.log(`[tooltip 诊断] dialogs=${await desktop.evaluate(() => [...document.querySelectorAll('dialog[open]')].map((d) => d.id).join(',') || '(none)')}`);
      console.log(`[tooltip 诊断] events=${await desktop.evaluate(() => window.__evts.slice(-18).join(' | '))}`);
      console.log(`[tooltip 诊断] hit=${await desktop.evaluate(() => {
        const box = document.querySelector('#usageDistBox');
        const rects = [...(box?.querySelectorAll('svg rect') || [])].filter((r) => r.getAttribute('fill') === 'transparent');
        const b0 = rects[0]?.getBoundingClientRect();
        return JSON.stringify({
          boxRect: box?.getBoundingClientRect().toJSON(),
          hitCount: rects.length,
          firstHit: b0 ? { x: Math.round(b0.x), y: Math.round(b0.y), w: Math.round(b0.width), h: Math.round(b0.height) } : null,
          vw: window.innerWidth, vh: window.innerHeight,
          panelHidden: document.querySelector('[data-settings-panel="usage"]')?.hidden,
        });
      })}`);
    }
    check('tooltip：hover 分布图出浮层（挂 settingsDialog 内，不被 top layer 盖住）', tooltipShown);

    // 筛选弹窗：打开 → 粒度切「天」→ 应用 → 工具行 select 同步 + 图表重绘。
    await desktop.evaluate(() => document.querySelector('#usageFilterOpen').click());
    await desktop.waitForSelector('#usageFilterDialog[open]', { timeout: 4000 });
    await desktop.evaluate(() => {
      const gran = document.querySelector('#usageFilterGran');
      gran.value = 'day';
      gran.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await desktop.evaluate(() => document.querySelector('#usageFilterApply').click());
    await desktop.waitForFunction(() => document.querySelector('#usageFilterDialog') && !document.querySelector('#usageFilterDialog').open, undefined, { timeout: 4000 });
    // 应用后 loadUsageStats 是异步 void：等拉数+重渲染完成（工具行 select 由
    // usageSyncWindowControls 在渲染末尾同步）再断言，否则拿到的是旧值。
    await desktop.waitForFunction(() => document.querySelector('#usageGranSel')?.value === 'day', undefined, { timeout: 8000 });
    const afterFilter = await desktop.evaluate(() => ({
      gran: document.querySelector('#usageGranSel')?.value,
      dist: Boolean(document.querySelector('#usageDistBox svg')),
    }));
    check('筛选弹窗：粒度切「天」应用后工具行同步 + 图表重绘',
      afterFilter.gran === 'day' && afterFilter.dist, JSON.stringify(afterFilter));
    // 切回小时粒度（后续手机段与偏好段用默认口径）。
    await desktop.evaluate(() => {
      const gran = document.querySelector('#usageGranSel');
      gran.value = 'hour';
      gran.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await desktop.waitForFunction(() => Boolean(document.querySelector('#usageDistBox svg')), undefined, { timeout: 8000 });

    // 偏好弹窗：默认范围存 7 天 → POST /api/settings 落库 → 工具行 7 天 active。
    await desktop.evaluate(() => document.querySelector('#usagePrefsOpen').click());
    await desktop.waitForSelector('#usagePrefsDialog[open]', { timeout: 4000 });
    await desktop.evaluate(() => {
      const range = document.querySelector('#usagePrefsRange');
      range.value = '7';
    });
    await desktop.evaluate(() => document.querySelector('#usagePrefsSave').click());
    await desktop.waitForFunction(() => document.querySelector('#usagePrefsDialog') && !document.querySelector('#usagePrefsDialog').open, undefined, { timeout: 6000 });
    await desktop.waitForFunction(() => document.querySelector('[data-usage-range="7"]')?.classList.contains('active'), undefined, { timeout: 8000 });
    const savedPrefs = (await apiJson('/api/bootstrap')).settings?.usage_dash;
    check('偏好弹窗：usage_dash 落服务端（range=7）且工具行跟随',
      savedPrefs?.range === '7' && savedPrefs?.gran === 'hour', JSON.stringify(savedPrefs));

    /* 供应商弹层：单价回填 + 保存后重开仍在（回归段，与本次改动无关但必须不退化）。 */
    await desktop.evaluate(() => {
      [...document.querySelectorAll('.provider-card')].find((card) => card.textContent.includes('冒烟 DeepSeek')).click();
    });
    await desktop.waitForSelector('#providerDialog[open]', { timeout: 5000 });
    const pricing = await desktop.evaluate(() => ({
      input: document.querySelector('#providerPriceInput')?.value || '',
      cached: document.querySelector('#providerPriceCachedInput')?.value || '',
      output: document.querySelector('#providerPriceOutput')?.value || '',
      currency: document.querySelector('#providerPriceCurrency')?.value || '',
    }));
    check('供应商弹层：三档单价与币种回填',
      pricing.input === '2' && pricing.cached === '0.4' && pricing.output === '8' && pricing.currency === '¥',
      JSON.stringify(pricing));
    await desktop.keyboard.press('Escape');
    await desktop.close();

    /* ══ 手机视图：软键盘桩下弹窗 max-height 压进可视区（用户点名的重点） ══ */
    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    await mobile.goto(BASE, { waitUntil: 'domcontentloaded' });
    // 同桌面段：写「已跳过引导」标记再重载，防引导弹窗中途盖到 top layer。
    await mobile.evaluate(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
    await mobile.reload({ waitUntil: 'domcontentloaded' });
    await mobile.waitForFunction(() => Boolean(window.__naibaReady || document.querySelector('#openSettings')), undefined, { timeout: 15000 });
    await mobile.evaluate(() => document.querySelector('#openSettings').click());
    await openUsageTab(mobile);

    // 桩 visualViewport（模拟软键盘弹出：可视区高 300、上移 100）。fake 对象带
    // add/removeEventListener——打开弹窗时的 vv 监听要挂上去。
    await mobile.evaluate(() => {
      const listeners = { resize: [], scroll: [] };
      const fake = {
        height: 300,
        offsetTop: 100,
        addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener(type, fn) {
          listeners[type] = (listeners[type] || []).filter((item) => item !== fn);
        },
        __emit(type) { for (const fn of listeners[type] || []) fn(); },
        __count(type) { return (listeners[type] || []).length; },
      };
      window.__fakeVV = fake;
      Object.defineProperty(window, 'visualViewport', { configurable: true, get: () => fake });
    });
    await mobile.evaluate(() => document.querySelector('#usageFilterOpen').click());
    await mobile.waitForSelector('#usageFilterDialog[open]', { timeout: 4000 });
    const mobileBounds = await mobile.evaluate(() => {
      const dlg = document.querySelector('#usageFilterDialog');
      const inline = dlg.style.maxHeight;
      return {
        inlineMaxHeight: inline,
        inlinePx: inline ? Number.parseInt(inline, 10) : 0,
        vvHeight: window.visualViewport.height,
        vvOffsetTop: window.visualViewport.offsetTop,
        vvResizeListeners: window.__fakeVV.__count('resize'),
      };
    });
    // 口径：avail = max(240, min(vv.height, innerHeight - vv.offsetTop) - 16)
    //      = max(240, min(300, 744) - 16) = 284 → dialog 不再延伸进键盘区。
    check('手机弹层：软键盘态 dialog max-height 压到 284px（300 可视高 − 16）+ vv 监听挂上',
      mobileBounds.inlinePx === 284 && mobileBounds.vvResizeListeners > 0, JSON.stringify(mobileBounds));
    // 键盘高度变化（vv resize 事件）→ max-height 跟随重算。
    await mobile.evaluate(() => {
      window.__fakeVV.height = 220;
      window.__fakeVV.__emit('resize');
    });
    const shrunk = await mobile.evaluate(() => Number.parseInt(document.querySelector('#usageFilterDialog').style.maxHeight, 10));
    // 220 可视高理论 204，但产品口径有 240px 下限（max(240, …)）防止键盘极高
    // 时弹窗被压没 ⇒ 240 才是正确值，204 是没算下限的错误期望。
    check('手机弹层：可视区再缩（220 高）→ 触 240px 下限兜底', shrunk === 240, `maxHeight=${shrunk}`);
    // 关闭弹窗：显式 dlg.close() 走原生 close 事件（摘监听 + 清内联 max-height）。
    // 不用 Esc——headless 手机仿真下 Esc-cancel 不可靠（探针实测 cancel/close 都
    // 不触发、焦点在弹窗内按钮上依旧），而这里要验的是 app 侧的 close 契约。
    await mobile.evaluate(() => document.querySelector('#usageFilterDialog').close());
    // close 事件是**异步派发**的（close() 同步只置 open=false，事件进 task queue）
    // ⇒ 等 handler 跑完（inline 被清空）再断言，否则读到执行前旧值；这个迟到的
    // handler 还会把之后打开的偏好弹窗刚设好的 max-height 一起清掉，所以这里
    // 等待是后续偏好断言稳定的前提。
    let closeHandled = false;
    try {
      await mobile.waitForFunction(() => document.querySelector('#usageFilterDialog').style.maxHeight === '', undefined, { timeout: 4000 });
      closeHandled = true;
    } catch (_) { /* timed out */ }
    const unbound = await mobile.evaluate(() => window.__fakeVV.__count('resize') === 0 && window.__fakeVV.__count('scroll') === 0);
    check('手机弹层：close 事件清内联 max-height + 摘 vv 监听（交给 CSS dvh 兜底）',
      closeHandled && unbound, JSON.stringify({ closeHandled, unbound }));
    // 偏好弹窗同款适配（上一用例把 fake vv.height 改成 220，先复位 300 再开，
    // 否则 240 恰是 height=220 的「正确值」、断言 284 反而假红）。
    await mobile.evaluate(() => { window.__fakeVV.height = 300; });
    await mobile.evaluate(() => document.querySelector('#usagePrefsOpen').click());
    await mobile.waitForSelector('#usagePrefsDialog[open]', { timeout: 4000 });
    const prefsBounds = await mobile.evaluate(() => Number.parseInt(document.querySelector('#usagePrefsDialog').style.maxHeight, 10) || 0);
    check('手机弹层：偏好弹窗同样压进可视区（284px）', prefsBounds === 284, `maxHeight=${prefsBounds}`);
    await mobile.evaluate(() => document.querySelector('#usagePrefsDialog').close());
    await mobile.close();

    await browser.close();
  } finally {
    stopServer();
  }

  if (failures.length) {
    console.error(`\n${failures.length} 项失败：\n- ${failures.join('\n- ')}`);
    process.exit(1);
  }
  console.log('\n全部通过');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
