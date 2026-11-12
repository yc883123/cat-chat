// 侧栏「批量管理」冒烟：隔离源码实例（verify/_serve_sidebar_batch.py，端口 8791）。
// 运行：$env:NODE_PATH="<node_modules 目录>"; node verify\sidebar_batch_smoke.cjs
// 手机：$env:NAIBA_SMOKE_MOBILE="1"; node verify\sidebar_batch_smoke.cjs
// 每个关键步骤后都查 pageerror / console.error（维护说明 9.167：静态检查拦不住运行时报错）。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8791';
const MOBILE = process.env.NAIBA_SMOKE_MOBILE === '1';
const failures = [];
const scriptErrors = [];

function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

// 当场判失败：等到元素超时再汇总，页面前面的报错就永远看不到了。
function checkNoScriptError(label) {
  if (!scriptErrors.length) return;
  check(`无脚本错误（${label}）`, false, scriptErrors.splice(0).join(' | '));
}

async function apiJson(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  return response.json().catch(() => ({}));
}

async function conversationTitles() {
  const payload = await apiJson('/api/conversations');
  return (payload.conversations || []).map((item) => String(item.title || ''));
}

async function conversationByTitle(title) {
  const payload = await apiJson('/api/conversations');
  return (payload.conversations || []).find((item) => String(item.title || '') === title) || null;
}

async function openSidebarIfMobile(page) {
  if (!MOBILE) return;
  const opened = await page.evaluate(() => document.querySelector('#sidebar')?.classList.contains('open'));
  if (!opened) await page.click('#openSidebar').catch(() => {});
  await page.waitForTimeout(350);
}

// 虚拟列表：先回顶部，再按视口步进找到目标行（只向下滚会在目标位于上方时永远找不到）。
async function ensureVisible(page, title) {
  await page.evaluate(() => { document.querySelector('#sidebarWorkspaceTree').scrollTop = 0; });
  await page.waitForTimeout(200);
  for (let i = 0; i < 80; i++) {
    const state = await page.evaluate((needle) => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      if (!tree) return 'missing';
      const item = [...tree.querySelectorAll('.conversation-item')]
        .find((el) => (el.querySelector('.conversation-open')?.textContent || '') === needle);
      if (item) { item.scrollIntoView({ block: 'center' }); return 'found'; }
      const step = Math.max(120, tree.clientHeight * 0.8);
      if (tree.scrollTop + tree.clientHeight >= tree.scrollHeight - 1) return 'end';
      tree.scrollTop = Math.min(tree.scrollTop + step, tree.scrollHeight);
      return 'scroll';
    }, title);
    if (state === 'found') { await page.waitForTimeout(180); return true; }
    if (state === 'end' || state === 'missing') return false;
    await page.waitForTimeout(120);
  }
  return false;
}

async function rowState(page, title) {
  return page.evaluate((needle) => {
    const tree = document.querySelector('#sidebarWorkspaceTree');
    const item = [...tree.querySelectorAll('.conversation-item')]
      .find((el) => (el.querySelector('.conversation-open')?.textContent || '') === needle);
    if (!item) return null;
    const box = item.querySelector('.conversation-check');
    return {
      id: item.dataset.conversationId || '',
      group: item.dataset.group || '',
      checked: box ? box.getAttribute('aria-checked') : null,
      hasCheck: Boolean(box),
      hasStar: Boolean(item.querySelector('.conversation-star')),
      hasMore: Boolean(item.querySelector('.conversation-more')),
      hasBranch: Boolean(item.querySelector('.conversation-branch, .conversation-branch-count')),
      draggable: item.getAttribute('draggable'),
    };
  }, title);
}

async function clickRow(page, title) {
  const found = await ensureVisible(page, title);
  if (!found) return false;
  await page.evaluate((needle) => {
    const tree = document.querySelector('#sidebarWorkspaceTree');
    const item = [...tree.querySelectorAll('.conversation-item')]
      .find((el) => (el.querySelector('.conversation-open')?.textContent || '') === needle);
    item.querySelector('.conversation-open')?.click();
  }, title);
  await page.waitForTimeout(220);
  return true;
}

async function batchBars(page) {
  return page.evaluate(() => {
    const status = document.querySelector('#workspaceBatchStatus');
    const actions = document.querySelector('#workspaceBatchActions');
    return {
      statusVisible: Boolean(status && !status.hidden),
      actionsVisible: Boolean(actions && !actions.hidden),
      count: document.querySelector('#workspaceBatchCount')?.textContent || '',
      allChecked: document.querySelector('#workspaceBatchAll')?.getAttribute('aria-checked') || '',
      archiveLabel: document.querySelector('#workspaceBatchArchive')?.textContent || '',
      archiveDisabled: document.querySelector('#workspaceBatchArchive')?.disabled,
      deleteDisabled: document.querySelector('#workspaceBatchDelete')?.disabled,
      doneDisabled: document.querySelector('#workspaceBatchDone')?.disabled,
      checks: document.querySelectorAll('#sidebarWorkspaceTree .conversation-check').length,
    };
  });
}

async function selectedCount(page) {
  const text = await page.evaluate(() => document.querySelector('#workspaceBatchCount')?.textContent || '');
  const match = /已选 (\d+) 个/.exec(text);
  return match ? Number(match[1]) : null;
}

async function expandEverything(page) {
  // 展开全部工作区：虚拟列表只画窗口内的行，但行模型是完整的（勾选/全选都按行模型算）。
  for (let round = 0; round < 12; round++) {
    const collapsed = await page.evaluate(() => {
      const group = [...document.querySelectorAll('#sidebarWorkspaceTree .workspace-group')]
        .find((el) => !el.classList.contains('expanded'));
      if (!group) return null;
      group.querySelector('.workspace-group-header')?.click();
      return group.dataset.workspaceName || '';
    });
    if (collapsed === null) break;
    await page.waitForTimeout(220);
  }
  for (let round = 0; round < 12; round++) {
    const clicked = await page.evaluate(() => {
      const button = document.querySelector('#sidebarWorkspaceTree .workspace-showmore[data-action="show-more"]');
      if (!button) return false;
      button.click();
      return true;
    });
    if (!clicked) break;
    await page.waitForTimeout(220);
  }
}

async function setFilter(page, value) {
  await page.click('#workspaceSort');
  await page.waitForTimeout(150);
  await page.click(`#sidebarViewMenu [data-view-pref="filter"][data-view-value="${value}"]`);
  await page.waitForTimeout(350);
}

// 原生 confirm：先挂一次性监听再点击。直接 await click 会被对话框卡住。
async function clickWithDialog(page, selector, accept = true) {
  let message = '';
  page.once('dialog', async (dialog) => {
    message = dialog.message();
    if (accept) await dialog.accept();
    else await dialog.dismiss();
  });
  await page.click(selector);
  await page.waitForTimeout(300);
  return message;
}

async function enterBatch(page) {
  const active = await page.evaluate(() => Boolean(document.querySelector('#workspaceBatchStatus') && !document.querySelector('#workspaceBatchStatus').hidden));
  if (!active) await page.click('#workspaceBatchToggle');
  await page.waitForTimeout(300);
}

async function desktopPass(page) {
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
  await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
  await page.waitForTimeout(700);
  checkNoScriptError('页面加载');

  // 1) 进入批量模式
  const before = await page.evaluate(() => ({
    active: document.querySelector('.conversation-item.active')?.dataset.conversationId || '',
  }));
  await enterBatch(page);
  let bars = await batchBars(page);
  const first = await rowState(page, '批量冒烟U-06');
  check('进入批量模式：状态行与操作栏出现', bars.statusVisible && bars.actionsVisible, JSON.stringify(bars));
  check('行左边换成勾选框', Boolean(first && first.hasCheck), JSON.stringify(first));
  check('「＋ 新会话」不显示', await page.evaluate(() => document.querySelectorAll('#sidebarWorkspaceTree .workspace-new-chat').length) === 0);
  check('星标 / ⋯ 收起', Boolean(first && !first.hasStar && !first.hasMore), JSON.stringify(first));
  checkNoScriptError('进入批量模式');

  // 2) 点一行：勾上、计数 1、当前会话不切换
  await clickRow(page, '批量冒烟U-05');
  bars = await batchBars(page);
  const after = await page.evaluate(() => ({
    active: document.querySelector('.conversation-item.active')?.dataset.conversationId || '',
  }));
  const picked = await rowState(page, '批量冒烟U-05');
  check('点行勾上且计数为 1', picked?.checked === 'true' && bars.count === '已选 1 个', `${JSON.stringify(picked)} ${bars.count}`);
  check('点行不打开对话（当前会话不变）', before.active === after.active, `${before.active} -> ${after.active}`);
  checkNoScriptError('点行勾选');

  // 3) 滚到底再滚回来，勾还在
  await page.evaluate(() => { const tree = document.querySelector('#sidebarWorkspaceTree'); tree.scrollTop = tree.scrollHeight; });
  await page.waitForTimeout(300);
  await page.evaluate(() => { document.querySelector('#sidebarWorkspaceTree').scrollTop = 0; });
  await page.waitForTimeout(300);
  await ensureVisible(page, '批量冒烟U-05');
  const afterScroll = await rowState(page, '批量冒烟U-05');
  check('滚动后勾选保留', afterScroll?.checked === 'true', JSON.stringify(afterScroll));
  checkNoScriptError('滚动保留勾选');

  // 4) 收藏的会话勾一处，另一处也亮，计数只加 1
  await expandEverything(page);
  await clickRow(page, '批量冒烟A-01');
  const dupCount = await selectedCount(page);
  await page.evaluate(() => { const tree = document.querySelector('#sidebarWorkspaceTree'); tree.scrollTop = tree.scrollHeight; });
  await page.waitForTimeout(350);
  const favoriteTwin = await page.evaluate(() => {
    const items = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item[data-group="__favorites__"]')];
    const row = items.find((el) => (el.querySelector('.conversation-open')?.textContent || '') === '批量冒烟A-01');
    return row ? row.querySelector('.conversation-check')?.getAttribute('aria-checked') : null;
  });
  check('收藏分组里的同一会话同步点亮', favoriteTwin === 'true', String(favoriteTwin));
  check('重复行计数只加 1', dupCount === 2, String(dupCount));
  checkNoScriptError('收藏去重');

  // 5) 全选 = 不重复的列出行数；收起一个分组后计数减少
  await page.evaluate(() => { document.querySelector('#sidebarWorkspaceTree').scrollTop = 0; });
  await page.waitForTimeout(250);
  await page.click('#workspaceBatchAll');
  await page.waitForTimeout(300);
  const allCount = await selectedCount(page);
  check('全选计数 = 列出的不重复会话数（20）', allCount === 20, String(allCount));
  await page.evaluate(() => {
    const header = document.querySelector('#sidebarWorkspaceTree .workspace-group[data-workspace-name="批量B"] .workspace-group-header');
    header?.click();
  });
  await page.waitForTimeout(350);
  const collapsedCount = await selectedCount(page);
  // 批量B 共 7 条，其中 B-07 已归档（隐藏档不列出）→ 收起后少 6。
  check('收起分组后计数相应减少（20-6=14）', collapsedCount === 14, String(collapsedCount));
  await page.click('#workspaceBatchAll'); // 全选 → 再点一次全部取消
  await page.waitForTimeout(250);
  check('再点全选 = 全部取消', (await selectedCount(page)) === 0, await selectedCount(page) + '');
  checkNoScriptError('全选');

  // 6) 搜索：不匹配的自动取消；搜索框 Esc 只清关键词
  await clickRow(page, '批量冒烟U-02');
  await page.click('#workspaceSearch');
  await page.waitForTimeout(200);
  await page.fill('#workspaceSearchInput', '批量冒烟U');
  await page.waitForTimeout(400);
  const filteredCount = await selectedCount(page);
  check('搜索后不在列表里的勾选自动取消', filteredCount === 1, String(filteredCount));
  await page.click('#workspaceBatchAll');
  await page.waitForTimeout(250);
  check('搜索状态下全选只选命中项（6）', (await selectedCount(page)) === 6, String(await selectedCount(page)));
  await page.focus('#workspaceSearchInput');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(350);
  const afterSearchEsc = await batchBars(page);
  check('搜索框按 Esc 只清关键词，批量模式还在', afterSearchEsc.statusVisible && afterSearchEsc.count !== '', JSON.stringify(afterSearchEsc));
  check('全文按钮置灰并给出原因', await page.evaluate(() => {
    const button = document.querySelector('#workspaceSearchModeFull');
    return Boolean(button?.disabled) && (button?.title || '') === '批量管理时只能按标题搜';
  }));
  checkNoScriptError('搜索与 Esc');

  // 7) 归档两条 → 切「仅显示已归档」→ 取消归档
  await page.click('#workspaceSearch'); // 关掉搜索行
  await page.waitForTimeout(250);
  await enterBatch(page);
  await page.evaluate(() => { document.querySelector('#sidebarWorkspaceTree').scrollTop = 0; });
  await page.waitForTimeout(250);
  await page.click('#workspaceBatchAll');
  await page.waitForTimeout(200);
  await page.click('#workspaceBatchAll'); // 清空选择
  await page.waitForTimeout(200);
  await clickRow(page, '批量冒烟U-02');
  await clickRow(page, '批量冒烟U-03');
  await page.click('#workspaceBatchArchive');
  await page.waitForTimeout(900);
  const archived02 = await conversationByTitle('批量冒烟U-02');
  const archived03 = await conversationByTitle('批量冒烟U-03');
  check('归档后服务端标记为 1', Number(archived02?.archived) === 1 && Number(archived03?.archived) === 1,
    JSON.stringify([archived02?.archived, archived03?.archived]));
  const toastArchive = await page.evaluate(() => document.querySelector('#toast')?.textContent || '');
  check('归档提示文案', /已归档 2 个/.test(toastArchive), toastArchive);
  const hiddenAfterArchive = await rowState(page, '批量冒烟U-02');
  check('「隐藏已归档」下归档项从列表消失', hiddenAfterArchive === null, JSON.stringify(hiddenAfterArchive));
  checkNoScriptError('归档');

  await setFilter(page, 'only');
  await page.click('#workspaceBatchAll');
  await page.waitForTimeout(300);
  const onlyBars = await batchBars(page);
  check('仅显示已归档 + 全选：按钮变成「取消归档」', onlyBars.archiveLabel === '取消归档', JSON.stringify(onlyBars));
  await page.click('#workspaceBatchArchive');
  await page.waitForTimeout(900);
  const restored02 = await conversationByTitle('批量冒烟U-02');
  check('取消归档后标记回到 0', Number(restored02?.archived) === 0, String(restored02?.archived));
  const toastUnarchive = await page.evaluate(() => document.querySelector('#toast')?.textContent || '');
  check('取消归档提示文案', /已取消归档/.test(toastUnarchive), toastUnarchive);
  await setFilter(page, 'hide');
  checkNoScriptError('取消归档');

  // 8) 删除三条：其中一条有任务 → 跳过并保持勾选
  await enterBatch(page);
  await page.evaluate(() => { document.querySelector('#sidebarWorkspaceTree').scrollTop = 0; });
  await page.waitForTimeout(250);
  await clickRow(page, '批量冒烟U-02');
  await clickRow(page, '批量冒烟U-04');
  await clickRow(page, '批量冒烟U-06');
  const dialogText = await clickWithDialog(page, '#workspaceBatchDelete', true);
  await page.waitForTimeout(1200);
  check('确认框列出数量', /删除选中的 3 个对话/.test(dialogText), dialogText);
  const gone02 = await conversationByTitle('批量冒烟U-02');
  const gone04 = await conversationByTitle('批量冒烟U-04');
  const busy06 = await conversationByTitle('批量冒烟U-06');
  check('空闲会话被删除', gone02 === null && gone04 === null, JSON.stringify([gone02, gone04]));
  check('有任务在跑的会话保留', Boolean(busy06), JSON.stringify(busy06));
  const busyRow = await rowState(page, '批量冒烟U-06');
  check('没删掉的保持勾选', busyRow?.checked === 'true', JSON.stringify(busyRow));
  const toastDelete = await page.evaluate(() => document.querySelector('#toast')?.textContent || '');
  check('删除提示说明跳过原因', /1 个还在回复或有任务在跑，没删/.test(toastDelete), toastDelete);
  checkNoScriptError('删除与跳过');

  // 9) 删除当前打开的对话 → 切到另一个还列着的会话
  await page.click('#workspaceBatchDone');
  await page.waitForTimeout(300);
  await clickRow(page, '批量冒烟U-05');
  await page.waitForTimeout(700);
  const openedId = await page.evaluate(() => document.querySelector('.conversation-item.active')?.dataset.conversationId || '');
  await enterBatch(page);
  await clickRow(page, '批量冒烟U-05');
  await clickWithDialog(page, '#workspaceBatchDelete', true);
  await page.waitForTimeout(1500);
  const nextActive = await page.evaluate(() => ({
    id: document.querySelector('.conversation-item.active')?.dataset.conversationId || '',
    title: document.querySelector('.conversation-item.active .conversation-open')?.textContent || '',
    messages: Boolean(document.querySelector('#messages') && document.querySelector('#messages').children.length),
  }));
  check('当前会话被删后切到另一个会话', Boolean(nextActive.id) && nextActive.id !== openedId, JSON.stringify(nextActive));
  check('切换后消息区有内容', nextActive.messages, JSON.stringify(nextActive));
  checkNoScriptError('删除当前会话');

  // 10) 确认框点取消什么都不变
  const beforeCancel = await conversationTitles();
  await clickRow(page, '批量冒烟U-01'); // U-02/U-04 已在上一步删掉，这里用仍存在的会话
  await clickWithDialog(page, '#workspaceBatchDelete', false);
  await page.waitForTimeout(700);
  const afterCancel = await conversationTitles();
  check('取消删除后会话不变', beforeCancel.length === afterCancel.length, `${beforeCancel.length} -> ${afterCancel.length}`);
  checkNoScriptError('取消删除');

  // 11) 完成 → 恢复普通列表；切到工作台再回来也已退出
  await page.click('#workspaceBatchDone');
  await page.waitForTimeout(350);
  const exited = await page.evaluate(() => ({
    status: document.querySelector('#workspaceBatchStatus')?.hidden,
    checks: document.querySelectorAll('#sidebarWorkspaceTree .conversation-check').length,
    stars: document.querySelectorAll('#sidebarWorkspaceTree .conversation-star').length,
  }));
  check('完成：勾选框消失、状态行隐藏', exited.status === true && exited.checks === 0, JSON.stringify(exited));
  check('完成：星标回来', exited.stars > 0, JSON.stringify(exited));
  await enterBatch(page);
  await page.click('#openStudio');
  await page.waitForTimeout(500);
  await page.click('#openChatMode');
  await page.waitForTimeout(600);
  const afterView = await batchBars(page);
  check('切到工作台再回来：已退出批量模式', afterView.statusVisible === false, JSON.stringify(afterView));
  checkNoScriptError('退出与视图切换');

  // 12) 最窄侧栏（170px）：四个图标在框内、状态行不横向溢出、「完成」完整可点
  await enterBatch(page);
  await page.evaluate(() => {
    document.documentElement.style.setProperty('--sidebar-w', '170px');
    window.dispatchEvent(new Event('resize'));
  });
  await page.waitForTimeout(500);
  const narrow = await page.evaluate(() => {
    const header = document.querySelector('#sidebarWorkspaceHeader');
    const status = document.querySelector('#workspaceBatchStatus');
    const done = document.querySelector('#workspaceBatchDone');
    const buttons = [...header.querySelectorAll('.icon-button')];
    const right = header.getBoundingClientRect().right;
    const doneRect = done.getBoundingClientRect();
    return {
      iconCount: buttons.length,
      iconsInside: buttons.every((button) => button.getBoundingClientRect().right <= right + 0.5),
      headerOverflow: header.scrollWidth - header.clientWidth,
      statusOverflow: status.scrollWidth - status.clientWidth,
      doneWidth: Math.round(doneRect.width),
      doneInside: doneRect.right <= status.getBoundingClientRect().right + 0.5,
    };
  });
  check('170px：四个图标都在标题行内', narrow.iconCount === 4 && narrow.iconsInside, JSON.stringify(narrow));
  check('170px：标题行不横向溢出', narrow.headerOverflow <= 1, JSON.stringify(narrow));
  check('170px：状态行不横向溢出且「完成」完整可点', narrow.statusOverflow <= 1 && narrow.doneWidth >= 24 && narrow.doneInside, JSON.stringify(narrow));
  checkNoScriptError('170px 布局');
  await page.evaluate(() => {
    document.documentElement.style.removeProperty('--sidebar-w');
    window.dispatchEvent(new Event('resize'));
  });
  await page.click('#workspaceBatchDone');
  await page.waitForTimeout(250);
}

async function mobilePass(browser) {
  const page = await browser.newPage({ viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true });
  page.on('pageerror', (err) => scriptErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
      if (msg.type() !== 'error') return;
      // 「Failed to load resource」不带 URL，真实来源由下面的 response 监听按精确路径判定。
      if (/^Failed to load resource/.test(msg.text())) return;
      scriptErrors.push(`console.error: ${msg.text()}`);
    });
  await page.addInitScript(() => localStorage.setItem('naibaOnboardingDismissed', '1'));
  await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
  await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
  await page.waitForTimeout(700);
  await openSidebarIfMobile(page);
  checkNoScriptError('手机：页面加载');

  await enterBatch(page);
  let bars = await batchBars(page);
  check('手机：状态行与操作栏出现', bars.statusVisible && bars.actionsVisible, JSON.stringify(bars));
  await clickRow(page, '批量冒烟U-01');
  check('手机：点行勾上', (await selectedCount(page)) === 1, String(await selectedCount(page)));
  // 关抽屉不清勾选
  await page.click('#sidebarBackdrop').catch(() => {});
  await page.waitForTimeout(400);
  await openSidebarIfMobile(page);
  const keptRow = await rowState(page, '批量冒烟U-01');
  check('手机：关抽屉再开，勾选保留', keptRow?.checked === 'true', JSON.stringify(keptRow));

  // 全选：展开全部后按行模型计数（与桌面同一口径：20 条未归档）。
  await expandEverything(page);
  await page.click('#workspaceBatchAll');
  await page.waitForTimeout(300);
  check('手机：全选计数 20', (await selectedCount(page)) === 20, String(await selectedCount(page)));
  await page.click('#workspaceBatchAll');
  await page.waitForTimeout(250);

  // 搜索：命中项之外自动取消勾选，Esc 只清关键词。
  await clickRow(page, '批量冒烟U-02');
  await page.click('#workspaceSearch');
  await page.waitForTimeout(250);
  await page.fill('#workspaceSearchInput', '批量冒烟U');
  await page.waitForTimeout(400);
  check('手机：搜索后勾选收拢到命中项', (await selectedCount(page)) === 1, String(await selectedCount(page)));
  await page.focus('#workspaceSearchInput');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(350);
  check('手机：搜索框 Esc 不退出批量模式', (await batchBars(page)).statusVisible, JSON.stringify(await batchBars(page)));
  await page.click('#workspaceSearch');
  await page.waitForTimeout(250);

  // 归档 → 仅显示已归档 → 取消归档（U-02 上一步已选中，这里只补 U-03）
  await clickRow(page, '批量冒烟U-03');
  await page.click('#workspaceBatchArchive');
  await page.waitForTimeout(1000);
  check('手机：归档写到服务端', Number((await conversationByTitle('批量冒烟U-02'))?.archived) === 1);
  await setFilter(page, 'only');
  await page.click('#workspaceBatchAll');
  await page.waitForTimeout(300);
  check('手机：取消归档按钮文案', (await batchBars(page)).archiveLabel === '取消归档', JSON.stringify(await batchBars(page)));
  await page.click('#workspaceBatchArchive');
  await page.waitForTimeout(1000);
  check('手机：取消归档恢复标记', Number((await conversationByTitle('批量冒烟U-02'))?.archived) === 0);
  await setFilter(page, 'hide');

  // 删除三条（一条有任务）：跳过并保持勾选
  await clickRow(page, '批量冒烟U-02');
  await clickRow(page, '批量冒烟U-04');
  await clickRow(page, '批量冒烟U-06');
  await clickWithDialog(page, '#workspaceBatchDelete', true);
  await page.waitForTimeout(1500);
  check('手机：空闲会话被删除', (await conversationByTitle('批量冒烟U-02')) === null
    && (await conversationByTitle('批量冒烟U-04')) === null);
  check('手机：有任务的会话保留', Boolean(await conversationByTitle('批量冒烟U-06')));
  await ensureVisible(page, '批量冒烟U-06');
  const busyMobile = await rowState(page, '批量冒烟U-06');
  check('手机：没删掉的保持勾选', busyMobile?.checked === 'true', JSON.stringify(busyMobile));

  await clickWithDialog(page, '#workspaceBatchDelete', false);
  await page.waitForTimeout(300);
  await page.click('#workspaceBatchDone');
  await page.waitForTimeout(350);
  const done = await page.evaluate(() => ({
    status: document.querySelector('#workspaceBatchStatus')?.hidden,
    checks: document.querySelectorAll('#sidebarWorkspaceTree .conversation-check').length,
  }));
  check('手机：完成退出', done.status === true && done.checks === 0, JSON.stringify(done));
  checkNoScriptError('手机：批量流程');
  await page.close();
}

(async () => {
  const titles = await conversationTitles();
  check('隔离实例已播种 22 条会话', titles.length === 22, String(titles.length));
  if (titles.length !== 22) process.exit(1);

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    if (!MOBILE) {
      const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
      page.on('pageerror', (err) => scriptErrors.push(`pageerror: ${err.message}`));
      page.on('console', (msg) => {
      if (msg.type() !== 'error') return;
      // 「Failed to load resource」不带 URL，真实来源由下面的 response 监听按精确路径判定。
      if (/^Failed to load resource/.test(msg.text())) return;
      scriptErrors.push(`console.error: ${msg.text()}`);
    });
      page.on('response', (res) => {
        const url = res.url();
        if (res.status() < 400) return;
        if (/\/favicon\.ico$/.test(url)) return;
        if (/\/api\/providers\/models$/.test(url)) return; // 占位供应商的模型探测（维护说明 6.1）
        scriptErrors.push(`http ${res.status()}: ${url}`);
      });
      // 占位供应商的模型探测会 4xx（维护说明 6.1）：只豁免这一条精确路径。
      await desktopPass(page);
      await page.close();
    } else {
      await mobilePass(browser);
    }
  } finally {
    await browser.close();
  }

  if (scriptErrors.length) check('无残留脚本错误', false, scriptErrors.join(' | '));
  console.log(failures.length ? `\n${failures.length} 项失败：${failures.join('；')}` : '\n全部通过');
  process.exit(failures.length ? 1 : 0);
})();
