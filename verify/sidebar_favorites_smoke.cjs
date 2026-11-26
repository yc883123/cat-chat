// 侧栏收藏 + 「⋯」菜单 + 滚轮步进 + 紧凑化冒烟（源码 server，端口 8790）。
// 前置：停掉源码 server 后先播种 —— python verify/seed_favorites.py
// 运行：$env:NODE_PATH="<node_modules 目录>"; node verify\sidebar_favorites_smoke.cjs
// 说明：本沙箱拦 Edge spawn，此脚本在放宽权限后执行（Edge 需可拉起）。
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  return response.json().catch(() => ({}));
}

// 虚拟列表：目标行不一定在 DOM 里，先回到顶部再按视口步进向下滚动直到出现
// （只向下滚会在目标位于列表上方时永远找不到）。
async function ensureVisible(page, title) {
  await page.evaluate(() => { document.querySelector('#sidebarWorkspaceTree').scrollTop = 0; });
  await page.waitForTimeout(250);
  for (let i = 0; i < 80; i++) {
    const state = await page.evaluate((needle) => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      if (!tree) return 'missing';
      const item = [...tree.querySelectorAll('.conversation-item')]
        .find((el) => (el.querySelector('.conversation-open')?.textContent || '').includes(needle));
      if (item) { item.scrollIntoView({ block: 'center' }); return 'found'; }
      const step = Math.max(120, tree.clientHeight * 0.8);
      if (tree.scrollTop + tree.clientHeight >= tree.scrollHeight - 1) return 'end';
      tree.scrollTop = Math.min(tree.scrollTop + step, tree.scrollHeight);
      return 'scroll';
    }, title);
    if (state === 'found') { await page.waitForTimeout(180); return true; }
    if (state === 'end' || state === 'missing') return false;
    await page.waitForTimeout(140);
  }
  return false;
}

async function favoritesView(page) {
  // 「已收藏」在列表最下方：滚到底部才能让它的行进入虚拟窗口。
  await page.evaluate(() => {
    const tree = document.querySelector('#sidebarWorkspaceTree');
    tree.scrollTop = tree.scrollHeight;
  });
  await page.waitForTimeout(350);
  return page.evaluate(() => {
    const items = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item[data-group="__favorites__"]')];
    return {
      count: items.length,
      titles: items.map((el) => el.querySelector('.conversation-open')?.textContent || ''),
    };
  });
}

async function itemSnapshot(page, title) {
  return page.evaluate((needle) => {
    const tree = document.querySelector('#sidebarWorkspaceTree');
    const item = [...tree.querySelectorAll('.conversation-item')]
      .find((el) => (el.querySelector('.conversation-open')?.textContent || '').includes(needle));
    if (!item) return null;
    const star = item.querySelector('.conversation-star');
    return {
      title: item.querySelector('.conversation-open')?.textContent || '',
      starFavorite: star ? star.classList.contains('is-favorite') : null,
      ariaPressed: star ? star.getAttribute('aria-pressed') : null,
      hasMore: Boolean(item.querySelector('.conversation-more')),
      height: item.getBoundingClientRect().height,
      groupName: item.dataset.group || '',
      fontSize: getComputedStyle(item.querySelector('.conversation-open')).fontSize,
    };
  }, title);
}

(async () => {
  const list = await apiJson('/api/conversations');
  const seeded = (list.conversations || []).filter((item) => String(item.title || '').startsWith('收藏冒烟'));
  const apiFavorites = seeded.filter((item) => Number(item.favorite || 0) === 1).map((item) => item.title);
  check('已播种「收藏冒烟」会话（>=35 条）', seeded.length >= 35, String(seeded.length));
  check('播种的已收藏跨工作区（2 条）', apiFavorites.length === 2, JSON.stringify(apiFavorites));
  if (seeded.length < 35) process.exit(1);

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const notFound = [];
  // 状态点红灯用例会故意 abort 请求，这类"网络失败"噪音单独收集，不算页面错误。
  const expectedNetworkNoise = (text) => /Failed to fetch|net::ERR_FAILED|ERR_FAILED|NetworkError/i.test(String(text));
  page.on('pageerror', (err) => { if (!expectedNetworkNoise(err.message)) pageErrors.push(`pageerror: ${err.message}`); });
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    if (expectedNetworkNoise(msg.text())) return;
    pageErrors.push(`console.error: ${msg.text()}`);
  });
  page.on('response', (res) => { if (res.status() === 404) notFound.push(res.url()); });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
    await page.waitForTimeout(600);

    // 1) 旧入口必须彻底消失
    const legacy = await page.evaluate(() => ({
      topbar: Boolean(document.querySelector('#openCurrentConversationSettings')),
      dialog: Boolean(document.querySelector('#conversationSettingsDialog')),
      gear: document.querySelectorAll('.conversation-settings').length,
      deleteBtn: document.querySelectorAll('.delete-conversation').length,
      promptField: Boolean(document.querySelector('#conversationSystemPrompt')),
      topNewChat: Boolean(document.querySelector('#newChatButton')),
      perGroupNewChat: document.querySelectorAll('#sidebarWorkspaceTree .workspace-new-chat').length,
    }));
    check('顶栏「对话设置」按钮已移除', !legacy.topbar, JSON.stringify(legacy));
    check('顶栏「新会话」按钮已移除（改用工作区内新建）', !legacy.topNewChat && legacy.perGroupNewChat > 0, JSON.stringify(legacy));
    check('对话设置对话框已移除', !legacy.dialog, JSON.stringify(legacy));
    check('会话条目齿轮已移除', legacy.gear === 0, JSON.stringify(legacy));
    check('条目内「删除」按钮已并入 ⋯ 菜单', legacy.deleteBtn === 0, JSON.stringify(legacy));
    check('会话级系统提示词输入框已移除', !legacy.promptField, JSON.stringify(legacy));

    // 2) 条目结构：五角星 + ⋯
    const structure = await page.evaluate(() => {
      const items = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item')];
      return {
        total: items.length,
        allStar: items.every((el) => el.querySelector('.conversation-star')),
        allMore: items.every((el) => el.querySelector('.conversation-more')),
      };
    });
    check('可视会话条目都带五角星', structure.allStar && structure.total > 0, JSON.stringify(structure));
    check('可视会话条目都带 ⋯ 按钮', structure.allMore && structure.total > 0, JSON.stringify(structure));

    // 3) 紧凑化：条目 34px、标题 12.5px
    const first = await page.evaluate(() => {
      const item = document.querySelector('#sidebarWorkspaceTree .conversation-item');
      return {
        height: item ? item.getBoundingClientRect().height : 0,
        fontSize: item ? getComputedStyle(item.querySelector('.conversation-open')).fontSize : '',
      };
    });
    check('条目高度 ≈ 34px', first.height >= 32 && first.height <= 36, String(first.height));
    check('会话标题字号 12.5px', first.fontSize === '12.5px', first.fontSize);

    // 4) 展开全部工作区，确认列表可滚动。
    // 注意：展开一次会整体重绘侧栏（innerHTML 替换），必须逐个重新查询后点击，
    // 不能在 evaluate 里 forEach 一个 NodeList（后续节点会变成游离节点）。
    for (let round = 0; round < 12; round++) {
      const collapsed = await page.evaluate(() => {
        const group = [...document.querySelectorAll('#sidebarWorkspaceTree .workspace-group')]
          .find((el) => !el.classList.contains('expanded'));
        if (!group) return null;
        group.querySelector('.workspace-group-header')?.click();
        return group.dataset.workspaceName || '';
      });
      if (collapsed === null) break;
      await page.waitForTimeout(250);
    }
    // 展开「展开其余 N 个会话」：滚轮步进测量需要真实的滚动范围
    for (let round = 0; round < 12; round++) {
      const clicked = await page.evaluate(() => {
        const button = document.querySelector('#sidebarWorkspaceTree .workspace-showmore[data-action="show-more"]');
        if (!button) return false;
        button.click();
        return true;
      });
      if (!clicked) break;
      await page.waitForTimeout(250);
    }
    await page.waitForTimeout(300);

    // 5)「已收藏」分组必须在最下方，且聚合跨工作区收藏
    // 虚拟列表只渲染可视窗口：先滚到底部，收藏分组与收藏项才会进入 DOM。
    await page.evaluate(() => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      tree.scrollTop = tree.scrollHeight;
    });
    await page.waitForTimeout(400);
    // 虚拟列表里行是扁平的：收藏项用 data-group="__favorites__" 定位。
    const favorites = await page.evaluate(() => {
      const groups = [...document.querySelectorAll('#sidebarWorkspaceTree .workspace-group')];
      const last = groups[groups.length - 1];
      const fav = document.querySelector('#sidebarWorkspaceTree .workspace-group[data-workspace-name="__favorites__"]');
      const items = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item[data-group="__favorites__"]')];
      return {
        groupCount: groups.length,
        lastIsFavorites: Boolean(last && last.dataset.workspaceName === '__favorites__'),
        lastName: last?.dataset.workspaceName || '',
        titles: items.map((el) => el.querySelector('.conversation-open')?.textContent || ''),
        hasDelete: fav ? Boolean(fav.querySelector('.workspace-delete')) : null,
      };
    });
    check('「已收藏」分组存在且位于侧栏最下方', favorites.lastIsFavorites, JSON.stringify(favorites));
    check('「已收藏」分组无删除工作区按钮', favorites.hasDelete === false, JSON.stringify(favorites));
    check(
      '「已收藏」聚合两个工作区的会话',
      favorites.titles.length === 2
        && apiFavorites.every((title) => favorites.titles.includes(title)),
      JSON.stringify(favorites.titles),
    );

    // 6) 滚轮步进：鼠标格 ≈1.5 倍，触控板小步进不加速
    const box = await page.evaluate(() => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      tree.scrollTop = Math.round((tree.scrollHeight - tree.clientHeight) / 2);
      const rect = tree.getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, scrollHeight: tree.scrollHeight, clientHeight: tree.clientHeight };
    });
    check('列表可滚动（用于步进测量）', box.scrollHeight > box.clientHeight + 100, JSON.stringify(box));
    await page.waitForTimeout(400);
    await page.mouse.move(box.x, box.y);
    const readScroll = () => page.evaluate(() => document.querySelector('#sidebarWorkspaceTree').scrollTop);
    const notchBefore = await readScroll();
    await page.mouse.wheel(0, 100);
    await page.waitForTimeout(350);
    const notchAfter = await readScroll();
    check('滚轮一格 ≈150px（1.5 倍）', notchAfter - notchBefore >= 140 && notchAfter - notchBefore <= 160,
      JSON.stringify({ before: notchBefore, after: notchAfter, delta: notchAfter - notchBefore }));
    const smallBefore = await readScroll();
    await page.mouse.wheel(0, 12);
    await page.waitForTimeout(350);
    const smallAfter = await readScroll();
    check('触控板小步进不被加速（交给原生）', smallAfter - smallBefore >= 8 && smallAfter - smallBefore <= 16,
      JSON.stringify({ before: smallBefore, after: smallAfter, delta: smallAfter - smallBefore }));

    // 7) 收藏：点星 → 进入「已收藏」并落库；**且不得重排工作区内的会话顺序**
    const target = '收藏冒烟 B-01';
    check(`滚动定位到「${target}」`, await ensureVisible(page, target), target);
    const groupOrder = () => page.evaluate(() => [...document.querySelectorAll(
      '#sidebarWorkspaceTree .conversation-item[data-group="收藏冒烟B"] .conversation-open')].map((el) => el.textContent));
    const orderBefore = await groupOrder();
    let before = await itemSnapshot(page, target);
    check('目标会话初始未收藏', before && before.starFavorite === false, JSON.stringify(before));
    const updatedBefore = (await apiJson('/api/conversations')).conversations
      ?.find((item) => String(item.title || '').includes(target))?.updated_at;
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${target}") .conversation-star`);
    await page.waitForTimeout(700);
    let after = await itemSnapshot(page, target);
    check('点击五角星后星标点亮', after && after.starFavorite === true && after.ariaPressed === 'true', JSON.stringify(after));
    const persisted = await apiJson('/api/conversations');
    const stored = (persisted.conversations || []).find((item) => String(item.title || '').includes(target));
    check('收藏已落库（favorite=1）', stored && Number(stored.favorite) === 1, JSON.stringify(stored || {}).slice(0, 120));
    check('收藏不推进 updated_at（否则侧栏会重排）',
      stored && String(stored.updated_at) === String(updatedBefore),
      JSON.stringify({ before: updatedBefore, after: stored && stored.updated_at }));
    const orderAfter = await groupOrder();
    check('收藏后工作区内会话顺序不变', JSON.stringify(orderAfter) === JSON.stringify(orderBefore),
      JSON.stringify({ before: orderBefore, after: orderAfter }));
    const favAfterStar = await favoritesView(page);
    check('「已收藏」分组变为 3 条', favAfterStar.count === 3, JSON.stringify(favAfterStar));
    check('收藏区出现该会话', favAfterStar.titles.includes(target), JSON.stringify(favAfterStar.titles));

    // 8) 在「已收藏」分组里取消收藏：从收藏区消失，但原工作区仍在
    const inFav = await page.evaluate((needle) => {
      const item = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item[data-group="__favorites__"]')]
        .find((el) => (el.querySelector('.conversation-open')?.textContent || '').includes(needle));
      if (item) item.scrollIntoView({ block: 'center' });
      return Boolean(item);
    }, target);
    check('收藏区里能看到该会话（可点击）', inFav, target);
    if (inFav) {
      await page.click(`#sidebarWorkspaceTree .conversation-item[data-group="__favorites__"]:has-text("${target}") .conversation-star`);
      await page.waitForTimeout(700);
      const favAfterUnstar = await favoritesView(page);
      check('取消收藏后从收藏区移除（回到 2 条）', favAfterUnstar.count === 2, JSON.stringify(favAfterUnstar));
      const stillInWorkspace = await ensureVisible(page, target);
      check('取消收藏后仍留在原工作区分组', stillInWorkspace, target);
      const back = await itemSnapshot(page, target);
      check('星标恢复未收藏态', back && back.starFavorite === false, JSON.stringify(back));
    }

    // 9) ⋯ 菜单：重命名
    const renameTarget = '收藏冒烟 B-02';
    check(`滚动定位到「${renameTarget}」`, await ensureVisible(page, renameTarget), renameTarget);
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${renameTarget}") .conversation-more`);
    await page.waitForTimeout(400);
    const menu = await page.evaluate(() => {
      const el = document.querySelector('#conversationItemMenu');
      const rect = el ? el.getBoundingClientRect() : null;
      return {
        hidden: el ? el.hidden : null,
        items: el ? [...el.querySelectorAll('[data-conversation-action]')].map((b) => b.textContent.trim()) : [],
        inViewport: rect ? (rect.left >= 0 && rect.top >= 0 && rect.right <= window.innerWidth + 1 && rect.bottom <= window.innerHeight + 1) : false,
        parentIsBody: el ? el.parentElement === document.body : false,
      };
    });
    check('⋯ 菜单打开且挂到 body（不被侧栏裁剪）', menu.hidden === false && menu.parentIsBody, JSON.stringify(menu));
    check('菜单含 重命名 / 删除', menu.items.join(',') === '重命名,删除', JSON.stringify(menu.items));
    check('菜单完整落在视口内', menu.inViewport, JSON.stringify(menu));

    await page.click('#conversationItemMenu [data-conversation-action="rename"]');
    await page.waitForSelector('#renameConversationDialog[open]', { timeout: 5000 });
    await page.fill('#renameConversationInput', `${renameTarget} 已改名`);
    await page.click('#saveRenameConversation');
    await page.waitForTimeout(800);
    const renamed = await page.evaluate(() => {
      const dialog = document.querySelector('#renameConversationDialog');
      return { open: dialog ? dialog.open : null };
    });
    check('重命名对话框已关闭', renamed.open === false, JSON.stringify(renamed));
    const renamedList = await apiJson('/api/conversations');
    const renamedRow = (renamedList.conversations || []).find((item) => String(item.title || '').includes('已改名'));
    check('重命名已落库', Boolean(renamedRow), JSON.stringify((renamedList.conversations || []).map((i) => i.title).filter((t) => t.includes('B-02'))));
    check('重命名后侧栏显示新标题', await ensureVisible(page, '已改名'), '已改名');

    // 10) 菜单关闭：Esc + 点击外部
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("已改名") .conversation-more`);
    await page.waitForTimeout(300);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    const closedByEsc = await page.evaluate(() => document.querySelector('#conversationItemMenu').hidden);
    check('Esc 关闭 ⋯ 菜单', closedByEsc === true, String(closedByEsc));
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("已改名") .conversation-more`);
    await page.waitForTimeout(300);
    await page.mouse.click(640, 40);
    await page.waitForTimeout(300);
    const closedByOutside = await page.evaluate(() => document.querySelector('#conversationItemMenu').hidden);
    check('点击外部关闭 ⋯ 菜单', closedByOutside === true, String(closedByOutside));

    // 11) 悬浮不得改变行高（虚拟列表行高是启动时量一次缓存的，突变会整列抖动）
    const headerHeight = () => page.evaluate(() => {
      const el = document.querySelector('#sidebarWorkspaceTree .workspace-group-header');
      return el ? Math.round(el.getBoundingClientRect().height * 100) / 100 : null;
    });
    const itemHeight = () => page.evaluate(() => {
      const el = document.querySelector('#sidebarWorkspaceTree .conversation-item');
      return el ? Math.round(el.getBoundingClientRect().height * 100) / 100 : null;
    });
    const headerIdle = await headerHeight();
    const itemIdle = await itemHeight();
    await page.hover('#sidebarWorkspaceTree .workspace-group-header');
    await page.waitForTimeout(250);
    const headerHover = await headerHeight();
    check('悬浮工作区分组表头高度不变', headerIdle === headerHover, JSON.stringify({ idle: headerIdle, hover: headerHover }));
    await page.hover('#sidebarWorkspaceTree .conversation-item');
    await page.waitForTimeout(250);
    const itemHover = await itemHeight();
    check('悬浮会话条目高度不变', itemIdle === itemHover, JSON.stringify({ idle: itemIdle, hover: itemHover }));

    // 12) 滚动时只按「行区间变化」重绘（每帧重写 innerHTML 是迟滞的主因）
    const mutations = await page.evaluate(async () => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      tree.scrollTop = 0;
      await new Promise((resolve) => setTimeout(resolve, 250));
      let count = 0;
      const observer = new MutationObserver((records) => { count += records.length; });
      observer.observe(tree, { childList: true, subtree: false });
      for (let i = 0; i < 20; i++) {
        tree.scrollTop += 8;
        await new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)));
      }
      observer.disconnect();
      return count;
    });
    // 20 帧 × 8px = 160px，行高 34px → 最多换 6 次窗口；旧实现是每帧一次（20）。
    check('滚动 160px 的重绘次数 ≤ 8（区间变化才重绘）', mutations <= 8, String(mutations));

    // 13) 点击会话条目不得把列表强制滚到顶（用户实测：视觉定位全丢）
    const scrollBefore = await page.evaluate(() => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      tree.scrollTop = Math.round((tree.scrollHeight - tree.clientHeight) / 2);
      return tree.scrollTop;
    });
    await page.waitForTimeout(400);
    const clickTarget = await page.evaluate(() => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      const viewTop = tree.scrollTop;
      const viewBottom = viewTop + tree.clientHeight;
      const item = [...tree.querySelectorAll('.conversation-item')].find((el) => {
        const rect = el.getBoundingClientRect();
        const treeRect = tree.getBoundingClientRect();
        return rect.top > treeRect.top + 10 && rect.bottom < treeRect.bottom - 10;
      });
      return item ? (item.querySelector('.conversation-open')?.textContent || '') : null;
    });
    check('找到视口中间的会话条目', Boolean(clickTarget), JSON.stringify({ scrollBefore, clickTarget }));
    if (clickTarget) {
      await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${clickTarget}") .conversation-open`);
      await page.waitForTimeout(1200);
      const scrollAfter = await page.evaluate(() => document.querySelector('#sidebarWorkspaceTree').scrollTop);
      check('点击会话后侧栏滚动位置不变（不再强制置顶）',
        Math.abs(scrollAfter - scrollBefore) <= 2,
        JSON.stringify({ before: scrollBefore, after: scrollAfter, target: clickTarget }));
      const stillVisible = await page.evaluate((needle) => {
        const item = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item')]
          .find((el) => (el.querySelector('.conversation-open')?.textContent || '').includes(needle));
        return Boolean(item && item.classList.contains('active'));
      }, clickTarget);
      check('被点击的会话标记为 active', stillVisible, clickTarget);
    }

    // 15) 收起按钮与搜索/排序/新建工作区同一行（不再单独占一行）
    const headerRow = await page.evaluate(() => {
      const actions = document.querySelector('.workspace-header-actions');
      const ids = ['workspaceSearch', 'workspaceSort', 'addWorkspace', 'collapseSidebar'];
      const rects = ids.map((id) => {
        const el = document.getElementById(id);
        if (!el) return null;
        const rect = el.getBoundingClientRect();
        return { id, top: Math.round(rect.top), centerY: Math.round(rect.top + rect.height / 2), left: Math.round(rect.left) };
      });
      return {
        inside: ids.every((id) => Boolean(actions?.querySelector(`#${id}`))),
        sidebarTopGone: !document.querySelector('.sidebar-top'),
        rects,
        sameRow: rects.every((r) => r && Math.abs(r.centerY - rects[0].centerY) <= 2),
      };
    });
    check('收起按钮已并入「工作区」操作行（同一行）', headerRow.inside && headerRow.sameRow, JSON.stringify(headerRow.rects));
    check('独立的 .sidebar-top 空行已移除', headerRow.sidebarTopGone, JSON.stringify(headerRow));
    await page.click('#collapseSidebar');
    await page.waitForTimeout(500);
    const collapsed = await page.evaluate(() => document.querySelector('#appShell').classList.contains('sidebar-collapsed'));
    check('收起按钮功能仍可用', collapsed === true, String(collapsed));
    await page.click('#expandSidebar');
    await page.waitForTimeout(600);
    check('展开按钮恢复侧栏', await page.evaluate(() => !document.querySelector('#appShell').classList.contains('sidebar-collapsed')), '');

    // 16) 会话条目整条可点（含上下边缘），且手型一致
    const edgeTarget = await page.evaluate(() => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      const item = [...tree.querySelectorAll('.conversation-item')].find((el) => !el.classList.contains('active'));
      if (!item) return null;
      item.scrollIntoView({ block: 'center' });
      const rect = item.getBoundingClientRect();
      return {
        title: item.querySelector('.conversation-open')?.textContent || '',
        x: Math.round(rect.left + rect.width * 0.6),
        topY: Math.round(rect.top + 1),
        bottomY: Math.round(rect.bottom - 2),
        cursor: getComputedStyle(item).cursor,
      };
    });
    check('找到非当前会话条目用于边缘点击', Boolean(edgeTarget), JSON.stringify(edgeTarget));
    if (edgeTarget) {
      check('条目整块为手型光标', edgeTarget.cursor === 'pointer', edgeTarget.cursor);
      const topHit = await page.evaluate(({ x, y }) => {
        const el = document.elementFromPoint(x, y);
        return el ? (el.closest('.conversation-item') ? 'item' : el.className) : 'none';
      }, { x: edgeTarget.x, y: edgeTarget.topY });
      check('条目上边缘命中条目本身', topHit === 'item', topHit);
      await page.mouse.click(edgeTarget.x, edgeTarget.topY);
      await page.waitForTimeout(900);
      const activated = await page.evaluate(() => document.querySelector('#sidebarWorkspaceTree .conversation-item.active .conversation-open')?.textContent || '');
      check('点击上边缘即切换会话', activated.includes(edgeTarget.title.slice(0, 8)), JSON.stringify({ want: edgeTarget.title, got: activated }));
    }

    // 17) 侧栏底部只剩一个状态点：绿=已连接，红=连不上服务端
    const dotState = await page.evaluate(() => {
      const dot = document.querySelector('#serverDot');
      return {
        exists: Boolean(dot),
        inAddressButton: Boolean(dot && dot.closest('#copyAddress')),
        label: document.querySelector('#serverLabel')?.textContent ?? null,
        serverStateGone: !document.querySelector('.server-state'),
        dots: document.querySelectorAll('#sidebarFooter .status-mark, #sidebarFooter .server-state i, .sidebar-footer i.status-mark').length,
        cls: dot ? dot.className : '',
      };
    });
    check('「服务已连接」文字与旧状态块已移除', dotState.label === null && dotState.serverStateGone, JSON.stringify(dotState));
    check('底部只保留一个状态点且在地址行内', dotState.exists && dotState.inAddressButton && dotState.dots === 1, JSON.stringify(dotState));
    check('初始为已连接（绿点）', dotState.cls.includes('connected') && !dotState.cls.includes('error'), dotState.cls);
    await page.route('**/api/conversations/**', (route) => route.abort('failed'));
    await page.click('#sidebarWorkspaceTree .conversation-item.active .conversation-open');
    await page.waitForTimeout(900);
    const redDot = await page.evaluate(() => {
      const dot = document.querySelector('#serverDot');
      return { cls: dot.className, bg: getComputedStyle(dot).backgroundColor };
    });
    check('连不上服务端时状态点变红', redDot.cls.includes('error') && !redDot.cls.includes('connected'), JSON.stringify(redDot));
    const debugBar = await page.evaluate(() => [...document.querySelectorAll('div')]
      .filter((el) => el.textContent.startsWith('PROMISE REJECT') || el.textContent.startsWith('JS ERROR'))
      .map((el) => el.textContent.slice(0, 200)).join(' || '));
    check('服务端不可用时只提示，不弹调试条', debugBar === '', debugBar);
    await page.unroute('**/api/conversations/**');
    await page.click('#sidebarWorkspaceTree .conversation-item.active .conversation-open');
    await page.waitForTimeout(900);
    const greenDot = await page.evaluate(() => {
      const dot = document.querySelector('#serverDot');
      return { cls: dot.className, bg: getComputedStyle(dot).backgroundColor };
    });
    check('恢复后状态点回到绿点', greenDot.cls.includes('connected') && !greenDot.cls.includes('error'), JSON.stringify(greenDot));

    // 18) 新建工作区走对话框（不再用 window.prompt）
    const workspaceName = '收藏冒烟新建区';
    const stubDir = path.join(__dirname, 'ws_smoke_dir');
    await page.route('**/api/workspace/pick', (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ path: stubDir, resolved: stubDir, cancelled: false }),
    }));
    await page.click('#addWorkspace');
    await page.waitForSelector('#newWorkspaceDialog[open]', { timeout: 8000 });
    const dialogState = await page.evaluate(() => ({
      tag: document.querySelector('#newWorkspaceDialog').tagName,
      hint: document.querySelector('#newWorkspaceDirHint').textContent,
      name: document.querySelector('#newWorkspaceName').value,
      hasSave: Boolean(document.querySelector('#saveNewWorkspace')),
    }));
    check('新建工作区弹出 <dialog>（不再用 window.prompt）', dialogState.tag === 'DIALOG', JSON.stringify(dialogState));
    check('对话框显示已选目录并预填名称', dialogState.hint.includes('ws_smoke_dir') && dialogState.name === 'ws_smoke_dir',
      JSON.stringify(dialogState));
    await page.fill('#newWorkspaceName', workspaceName);
    await page.click('#saveNewWorkspace');
    await page.waitForTimeout(1500);
    const created = await apiJson('/api/workspaces');
    const workspaceRow = (created.workspaces || []).find((item) => item.name === workspaceName);
    check('工作区已创建并落库', Boolean(workspaceRow), JSON.stringify(created.workspaces || []).slice(0, 160));
    const afterCreate = await page.evaluate(() => document.querySelector('#newWorkspaceDialog').open);
    check('创建后对话框关闭', afterCreate === false, String(afterCreate));
    // 清理：删掉该工作区与其下自动创建的新会话
    const listAfter = await apiJson('/api/conversations');
    for (const item of (listAfter.conversations || [])) {
      if (String(item.workspace_group || '') === workspaceName) {
        await apiJson(`/api/conversations/${item.id}`, { method: 'DELETE' });
      }
    }
    await apiJson('/api/workspaces/delete', { method: 'POST', body: JSON.stringify({ name: workspaceName }) });
    console.log(`（已清理冒烟工作区「${workspaceName}」）`);

    // 19) 收起侧栏不得放大对话列（此前 --chat-max 从 880px 跳到 ~1300px：
    //     助手区块左移、用户气泡右移，用户实测"整体不在中间还错开了"）
    const withMessages = (await apiJson('/api/conversations')).conversations?.find(
      (item) => String(item.title || '').includes('媒体内嵌冒烟'));
    if (withMessages && await ensureVisible(page, '媒体内嵌冒烟')) {
      await page.click('#sidebarWorkspaceTree .conversation-item:has-text("媒体内嵌冒烟") .conversation-open');
      await page.waitForSelector('#messages .message-row.user', { timeout: 20000 });
      await page.waitForTimeout(700);
      const layout = () => page.evaluate(() => {
        const shell = document.querySelector('#appShell');
        const assistant = document.querySelector('.message-row:not(.user) .message-body');
        const user = document.querySelector('.message-row.user .message-body');
        const rect = (el) => (el ? el.getBoundingClientRect() : null);
        const a = rect(assistant);
        const u = rect(user);
        return {
          chatMax: getComputedStyle(shell).getPropertyValue('--chat-max').trim(),
          assistantWidth: a ? Math.round(a.width) : null,
          span: (a && u) ? Math.round(u.right - a.left) : null,
        };
      });
      const expanded = await layout();
      await page.click('#collapseSidebar');
      await page.waitForTimeout(700);
      const collapsedLayout = await layout();
      await page.click('#expandSidebar');
      await page.waitForTimeout(700);
      check('收起侧栏后对话列宽保持恒定', collapsedLayout.chatMax === expanded.chatMax, `${expanded.chatMax} -> ${collapsedLayout.chatMax}`);
      check('收起侧栏后助手/用户相对布局不变（只重新居中）',
        expanded.assistantWidth !== null
        && Math.abs(collapsedLayout.assistantWidth - expanded.assistantWidth) <= 2
        && expanded.span !== null
        && Math.abs(collapsedLayout.span - expanded.span) <= 2,
        JSON.stringify({ expanded, collapsed: collapsedLayout }));
    } else {
      check('找到带消息的会话用于列宽校验', false, '缺少「媒体内嵌冒烟」播种数据');
    }

    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
    check('零 404 资源', notFound.length === 0, notFound.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`侧栏收藏冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
