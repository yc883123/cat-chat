// 第一批三项（全文搜索 / 分支导航 / 删除单条消息）冒烟（由 verify/batch1_smoke.py 起独立源码 server 后调用）。
//
// 覆盖：
//   ① 搜索：点放大镜 → 出现「标题｜全文」切换；切「全文」输入关键词 → 结果列表（共 N 条命中）；
//      片段含命中词且有 <mark> 高亮；点命中 → 切到命中所在会话并把那条消息滚进视口 + 高亮；
//      搜索是只读的（会话消息条数不变）；Esc 退回会话树；
//   ② 分支：源会话行有「⑂1」计数徽标、分支会话行有分支徽标；
//      点分支徽标 → 面板列出「源 + 兄弟」且当前项 is-current、源项 is-source、标题为「分支链（源 + 兄弟）」；
//      点源项能跳回源会话；在源会话点计数徽标 → 面板变成「这个会话的分支」（自己不在链里）；
//   ③ 删除：确认框按位置分级（user 有「仅删这一条 / 连同 AI 回复整轮删除」）；
//      「整轮删除」后后端恰好少两条；撤销条出现 → 点撤销 → 后端**逐字**复原（内容与顺序）；
//      assistant 只有「仅删这一条」；单删 + 撤销同样复原；刷新页面后撤销条不再（快照只存内存）；
//   ④ 零页面错误。
//
// 所有「生效」的断言都回读后端（`GET /api/conversations/<id>`），DOM 只用来断言交互是否到位 ——
// 前端把行删掉但后端没删（或反之）是本批最典型的回归。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8802';
const SRC_ID = process.env.NAIBA_SMOKE_SRC_ID || '';
const BRANCH_ID = process.env.NAIBA_SMOKE_BRANCH_ID || '';
const DEL_ID = process.env.NAIBA_SMOKE_DEL_ID || '';
const SRC_Q2_ID = process.env.NAIBA_SMOKE_SRC_Q2_ID || '';
const SRC_A2_ID = process.env.NAIBA_SMOKE_SRC_A2_ID || '';
const DEL_Q1_ID = process.env.NAIBA_SMOKE_DEL_Q1_ID || '';
const SRC_TITLE = process.env.NAIBA_SMOKE_SRC_TITLE || '';
const DEL_TITLE = process.env.NAIBA_SMOKE_DEL_TITLE || '';
const TOKEN = process.env.NAIBA_SMOKE_TOKEN || 'MAGICTOKEN';
const SRC_A2 = process.env.NAIBA_SMOKE_SRC_A2 || '';
const SHOTS = process.env.NAIBA_SMOKE_SHOTS || require('path').join(__dirname, '..', 'verify');

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}
function shot(name) {
  return require('path').join(SHOTS, `batch1_smoke_${name}.png`);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 940 } });
  const pageErrors = [];
  const httpErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`);
  });
  page.on('response', (response) => {
    if (response.status() >= 400) httpErrors.push(`${response.status()} ${response.url()}`);
  });

  const apiJson = async (path, options = {}) => {
    const init = { headers: { 'Content-Type': 'application/json' }, ...options };
    if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
    const response = await fetch(`${BASE}${path}`, init);
    return response.json().catch(() => ({}));
  };
  // 断言一律以后端为准：前端删除后必须真的少两条，撤销后必须逐字复原。
  const messagesOf = async (id) => (await apiJson(`/api/conversations/${id}`)).messages || [];
  const fingerprint = (list) => JSON.stringify(
    list.map((m) => [m.id, m.role, m.content, JSON.stringify(m.metadata || {})]),
  );

  async function waitFor(fn, timeout = 15000, step = 120) {
    const end = Date.now() + timeout;
    let last = null;
    while (Date.now() < end) {
      last = await fn();
      if (last) return last;
      await page.waitForTimeout(step);
    }
    return last;
  }

  const rowOf = (id) => `.conversation-item[data-conversation-id="${id}"]`;
  const msgRow = (id) => `#messages .message-row[data-message-id="${id}"]`;

  try {
    // 模型目录请求拦截：播种的假供应商 base_url 是 example.invalid，不拦截就会以 400 落地，
    // 在控制台留下 console.error 噪音（与 regenerate_smoke 同做法）。
    await page.route('**/api/providers/models', (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ models: [{ id: 'smoke-b1-pro', name: 'smoke-b1-pro' }] }),
    }));

    await page.goto(BASE, { waitUntil: 'domcontentloaded' });
    // 等侧栏真的画出会话行（虚拟列表，必须等到目标行在窗口内）。
    await page.waitForSelector(rowOf(SRC_ID), { timeout: 15000 });
    await page.waitForTimeout(400);

    // ============================================================ ① 搜索
    check('源会话在侧栏可见', await page.isVisible(rowOf(SRC_ID)));
    await page.click('#workspaceSearch');
    await page.waitForSelector('#workspaceSearchRow:not([hidden])', { timeout: 5000 });
    check('放大镜展开搜索行', await page.isVisible('#workspaceSearchInput'));
    check('默认「标题」模式', await page.getAttribute('#workspaceSearchModeTitle', 'aria-pressed') === 'true');

    await page.click('#workspaceSearchModeFull');
    check('切到「全文」模式',
      await page.getAttribute('#workspaceSearchModeFull', 'aria-pressed') === 'true'
      && await page.getAttribute('#workspaceSearchModeTitle', 'aria-pressed') === 'false');
    await page.fill('#workspaceSearchInput', TOKEN);
    await page.waitForSelector('.search-hit', { timeout: 15000 });
    await page.waitForTimeout(300);

    const hitCount = await page.locator('.search-hit').count();
    const headText = (await page.textContent('.search-results-head')) || '';
    check('全文命中渲染出结果', hitCount >= 1, `hits=${hitCount} head=${headText}`);
    check('结果头显示命中总数', /共\s*\d+\s*条命中/.test(headText), headText);
    const snippetHtml = (await page.innerHTML('.search-hit-snippet')) || '';
    check('片段含命中词且有 <mark> 高亮',
      snippetHtml.includes('<mark>') && snippetHtml.toLowerCase().includes(TOKEN.toLowerCase()),
      snippetHtml.slice(0, 120));
    check('片段来自原文（含上下文「藏在这里」）',
      ((await page.textContent('.search-hit-snippet')) || '').includes('藏在这里'));

    // 搜索是只读的：会话消息条数不能变。
    const srcBeforeSearch = await messagesOf(SRC_ID);
    await page.screenshot({ path: shot('1_search_results') });

    // 点命中 → 切会话 + 定位高亮（此时「当前会话」是自动打开的那个，未必是源会话）。
    // 注意：全文模式下侧栏被**结果列表替换**，会话树的行此刻不在 DOM 里，
    // 所以「切过去了」的判据是结果列表里那条命中被标成 active，而不是树行的 class。
    await page.click(`.search-hit[data-search-hit="${SRC_A2_ID}"]`);
    const located = await page.waitForSelector(`${msgRow(SRC_A2_ID)}.message-located`, { timeout: 15000 });
    check('点命中切到源会话并高亮那条消息', Boolean(located));
    check('切过去的是源会话',
      (await page.locator(`.search-hit[data-search-hit="${SRC_A2_ID}"].active`).count()) === 1);
    check('搜索只读：消息条数不变', (await messagesOf(SRC_ID)).length === srcBeforeSearch.length);
    await page.screenshot({ path: shot('2_hit_located') });

    // Esc 退出搜索，回到会话树（结果列表是替换式的，必须有键盘出口）。
    await page.click('#workspaceSearchInput');
    await page.press('#workspaceSearchInput', 'Escape');
    await page.waitForSelector('.search-results', { state: 'detached', timeout: 5000 }).catch(() => {});
    await page.waitForSelector(rowOf(SRC_ID), { timeout: 8000 });
    check('Esc 退回会话树', await page.isVisible(rowOf(SRC_ID)));
    check('退回来后源会话是当前会话',
      String(await page.getAttribute(rowOf(SRC_ID), 'class')).includes('active'));
    await page.click('#workspaceSearch');  // 收起搜索行，给后面的面板操作腾开视野

    // ======================================================== ② 分支导航
    const branchBadgeInBranch = await page.isVisible(`${rowOf(BRANCH_ID)} .conversation-branch`);
    const countBadgeInSource = (await page.textContent(`${rowOf(SRC_ID)} .conversation-branch-count`)) || '';
    check('分支会话行有分支徽标', branchBadgeInBranch);
    check('源会话行有分支计数徽标 ⑂1', countBadgeInSource.includes('⑂') && countBadgeInSource.includes('1'),
      countBadgeInSource);
    check('分支徽标 tooltip 指向源标题',
      String(await page.getAttribute(`${rowOf(BRANCH_ID)} .conversation-branch`, 'title')).includes(SRC_TITLE));

    await page.click(`${rowOf(BRANCH_ID)} .conversation-branch`);
    await page.waitForSelector('#branchChainPanel:not([hidden])', { timeout: 8000 });
    await page.waitForSelector('#branchChainList .branch-chain-item', { timeout: 8000 });
    const headLabel = (await page.textContent('#branchChainHeadText')) || '';
    const items = await page.$$eval('#branchChainList .branch-chain-item', (nodes) => nodes.map((n) => ({
      id: n.dataset.branchGoto || '',
      text: n.textContent || '',
      current: n.classList.contains('is-current'),
      source: n.classList.contains('is-source'),
    })));
    check('面板标题是「分支链（源 + 兄弟）」', headLabel.includes('分支链'), headLabel);
    check('链里同时有源与本分支', items.some((i) => i.id === SRC_ID) && items.some((i) => i.id === BRANCH_ID),
      JSON.stringify(items.map((i) => i.id)));
    check('源项标 is-source、当前项标 is-current',
      items.find((i) => i.id === SRC_ID)?.source === true
      && items.find((i) => i.id === BRANCH_ID)?.current === true,
      JSON.stringify(items));
    await page.screenshot({ path: shot('3_branch_chain_panel') });

    // 点源项跳过去 → 打开的是源会话。
    await page.click(`#branchChainList [data-branch-goto="${SRC_ID}"]`);
    await waitFor(async () => (await page.getAttribute(rowOf(SRC_ID), 'class') || '').includes('active'));
    check('点链里的源项能跳回源会话',
      String(await page.getAttribute(rowOf(SRC_ID), 'class')).includes('active'));
    check('跳转后面板关闭', await page.isHidden('#branchChainPanel'));

    // 源会话自己点计数徽标 → 「这个会话的分支」，自己不在链里。
    await page.click(`${rowOf(SRC_ID)} .conversation-branch-count`);
    await page.waitForSelector('#branchChainPanel:not([hidden])', { timeout: 8000 });
    await page.waitForSelector('#branchChainList .branch-chain-item', { timeout: 8000 });
    const srcItems = await page.$$eval('#branchChainList .branch-chain-item',
      (nodes) => nodes.map((n) => n.dataset.branchGoto || ''));
    check('源会话的链只列分支、不含自己',
      srcItems.includes(BRANCH_ID) && !srcItems.includes(SRC_ID), JSON.stringify(srcItems));
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    // ============================================================ ③ 删除
    await page.click(`${rowOf(DEL_ID)} .conversation-open`);
    await page.waitForSelector(msgRow(DEL_Q1_ID), { timeout: 15000 });
    await page.waitForTimeout(400);
    const delBefore = await messagesOf(DEL_ID);
    check('删除实验会话已载入 4 条消息', delBefore.length === 4, `len=${delBefore.length}`);
    const delFingerprint = fingerprint(delBefore);

    // user 消息：分级确认框要有两个选项。
    await page.click(`${msgRow(DEL_Q1_ID)} [data-delete-message]`);
    await page.waitForSelector('#messageDeleteDialog[open]', { timeout: 8000 });
    const notes = (await page.textContent('#messageDeleteNotes')) || '';
    check('确认框给出「仅删这一条」', await page.isVisible('#messageDeleteSingle'));
    check('user 消息额外给「连同 AI 回复整轮删除」', await page.isVisible('#messageDeleteTurn'));
    check('确认框说明整轮含义', notes.includes('整轮') || notes.includes('AI 回复'), notes);
    check('确认框写明可撤销', notes.includes('撤销') || (await page.textContent('#messageDeleteHint') || '').includes('撤销'));
    await page.screenshot({ path: shot('4_delete_confirm') });

    await page.click('#messageDeleteTurn');
    await page.waitForSelector('#messageDeleteDialog:not([open])', { timeout: 8000 }).catch(() => {});
    await page.waitForSelector('#undoBar:not([hidden])', { timeout: 8000 });
    const delAfterTurn = await messagesOf(DEL_ID);
    check('整轮删除后端恰好少两条', delAfterTurn.length === delBefore.length - 2,
      `before=${delBefore.length} after=${delAfterTurn.length}`);
    check('被删的是那条提问与它的回复',
      !delAfterTurn.some((m) => m.id === DEL_Q1_ID) && delAfterTurn.some((m) => m.content === '删除第二问'));
    check('DOM 同步移除了那一行', (await page.locator(msgRow(DEL_Q1_ID)).count()) === 0);
    await page.screenshot({ path: shot('5_undo_bar') });

    await page.click('#undoBarAction');
    await waitFor(async () => (await messagesOf(DEL_ID)).length === 4);
    check('撤销后后端回到 4 条', (await messagesOf(DEL_ID)).length === 4);
    check('撤销后内容与顺序逐字复原', fingerprint(await messagesOf(DEL_ID)) === delFingerprint);
    // 撤销条是「先淡出再 hidden」（220ms 收尾），所以这里要等它真的收起，不能立刻断言。
    await page.waitForSelector('#undoBar[hidden]', { timeout: 5000 }).catch(() => {});
    check('撤销后撤销条收起', await page.isHidden('#undoBar'));
    await page.waitForSelector(msgRow(DEL_Q1_ID), { timeout: 8000 });
    await page.screenshot({ path: shot('6_after_undo') });

    // assistant 消息：只有「仅删这一条」（不能整轮删一条回复）。
    const delA2Id = (await messagesOf(DEL_ID)).find((m) => m.role === 'assistant' && m.content === '删除第二答').id;
    await page.click(`${msgRow(delA2Id)} [data-delete-message]`);
    await page.waitForSelector('#messageDeleteDialog[open]', { timeout: 8000 });
    check('assistant 只给「仅删这一条」',
      await page.isVisible('#messageDeleteSingle') && await page.isHidden('#messageDeleteTurn'));
    await page.click('#messageDeleteSingle');
    await waitFor(async () => (await messagesOf(DEL_ID)).length === 3);
    check('单删一条后后端少一条', (await messagesOf(DEL_ID)).length === 3);
    check('单删只动了那一条', !(await messagesOf(DEL_ID)).some((m) => m.id === delA2Id));
    await page.waitForSelector('#undoBar:not([hidden])', { timeout: 8000 });
    await page.click('#undoBarAction');
    await waitFor(async () => (await messagesOf(DEL_ID)).length === 4);
    check('单删也能撤销复原', fingerprint(await messagesOf(DEL_ID)) === delFingerprint);

    // ======================================================== ④ 零页面错误
    // 本批新增的三个接口都不该在任何一步返回 4xx/5xx；预存的其它噪音单独列出来，
    // 免得把「本批回归」和「环境噪音」混为一谈。
    const ours = httpErrors.filter((line) => /search\/messages|branch_chain|messages\/(delete|restore)/.test(line));
    check('本批接口零 HTTP 错误', ours.length === 0, ours.join(' | '));
    check('页面无 JS 错误', pageErrors.length === 0, pageErrors.join(' | '));
    if (httpErrors.length) console.log(`[info] 其它 HTTP 错误（非本批）：${httpErrors.join(' | ')}`);
  } catch (error) {
    check('冒烟执行未抛异常', false, error && error.message ? error.message : String(error));
    try {
      const dump = await page.evaluate(() => ({
        activeConv: document.querySelector('.conversation-item.active')?.dataset.conversationId || '',
        rows: [...document.querySelectorAll('#messages .message-row')].map((r) => ({
          id: r.dataset.messageId || '', role: r.dataset.role || '',
          text: (r.textContent || '').replace(/\s+/g, ' ').slice(0, 30),
        })),
        searchRowHidden: document.querySelector('#workspaceSearchRow')?.hidden,
        searchHits: document.querySelectorAll('.search-hit').length,
        chainHidden: document.querySelector('#branchChainPanel')?.hidden,
        chainItems: document.querySelectorAll('#branchChainList .branch-chain-item').length,
        dialogOpen: document.querySelector('#messageDeleteDialog')?.open,
        deleteNotes: document.querySelector('#messageDeleteNotes')?.textContent || '',
        undoVisible: !document.querySelector('#undoBar')?.hidden,
      }));
      console.log(`\n[DEBUG 现场] ${JSON.stringify(dump, null, 2)}`);
      console.log(`[DEBUG 页面错误] ${pageErrors.join(' | ') || '（无）'}`);
    } catch (_) { /* 求值失败时忽略，至少保住原报错 */ }
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\n${failures.length} 项未通过` : '\n全部通过');
  process.exit(failures.length ? 1 : 0);
})();
