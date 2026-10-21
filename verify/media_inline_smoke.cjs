// 媒体就地内嵌冒烟（源码 server，端口 8799）。
// 前置：停掉源码 server 后先播种数据 —— python verify/seed_media_message.py
// 运行：$env:NODE_PATH="<node_modules 目录>"; node verify\media_inline_smoke.cjs
// 说明：本沙箱拦 Edge spawn，此脚本在用户本机（已装 playwright）执行。
//
// 2026-10-21 扩展（媒体附卡短注）：layout 中新增 show_media 消息段，断言——
//   ① 工具块结果文本含「已附媒体卡」短注（前后端所见一致：display run 的 result 尾行）；
//   ② 同一来源恰好 1 张就地媒体卡（短注不产生第二张卡，无重复展示）；
//   ③ 375px 手机视口同样可见（维护说明 §6：每次验收至少覆盖桌面与手机）。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const NOTE_LINE = '[媒体附卡] 本轮已附媒体卡 1 张：图片 seed_shown.png';
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

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 900 },
  { name: 'mobile', width: 375, height: 812 },
];

async function openMediaConversation(browser, viewport, conversation) {
  const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } });
  const errors = [];
  const notFound = [];
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`); });
  page.on('response', (res) => { if (res.status() === 404) notFound.push(res.url()); });
  await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
  await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
  // 手机视口：侧栏是离屏抽屉，必须先点顶栏菜单打开抽屉再定位会话。
  if (viewport.width <= 480) {
    await page.waitForSelector('#openSidebar', { timeout: 8000 });
    await page.click('#openSidebar');
    await page.waitForTimeout(500);
  }
  // 分组默认折叠 + 列表虚拟化：先展开全部分组，再按标题滚动定位后点开目标会话
  // （自动打开的是"最近更新"的会话，可能是别的播种数据）。
  for (let i = 0; i < 12; i++) {
    const clicked = await page.evaluate(() => {
      const group = [...document.querySelectorAll('#sidebarWorkspaceTree .workspace-group')]
        .find((el) => !el.classList.contains('expanded'));
      if (!group) return false;
      group.querySelector('.workspace-group-header')?.click();
      return true;
    });
    if (!clicked) break;
    await page.waitForTimeout(220);
  }
  for (let i = 0; i < 80; i++) {
    const state = await page.evaluate((needle) => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      const item = [...tree.querySelectorAll('.conversation-item')]
        .find((el) => (el.querySelector('.conversation-open')?.textContent || '').includes(needle));
      if (item) { item.scrollIntoView({ block: 'center' }); return 'found'; }
      const step = Math.max(120, tree.clientHeight * 0.8);
      if (tree.scrollTop + tree.clientHeight >= tree.scrollHeight - 1) return 'end';
      tree.scrollTop = Math.min(tree.scrollTop + step, tree.scrollHeight);
      return 'scroll';
    }, conversation.title);
    if (state === 'found' || state === 'end') break;
    await page.waitForTimeout(140);
  }
  await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${conversation.title}") .conversation-open`);
  await page.waitForTimeout(1200);
  await page.waitForSelector('#messages .message-row.assistant', { timeout: 20000 });
  return { page, errors, notFound };
}

async function snapshot(page) {
  return page.evaluate(() => {
    const rows = [...document.querySelectorAll('#messages .message-row.assistant')];
    const mediaRow = rows.find((row) => row.querySelector('.tool-media')) || null;
    const legacyRow = rows.find((row) => !row.querySelector('.tool-media')
      && row.querySelector('.message-body > .media-grid')) || null;
    const noteRun = rows
      .flatMap((row) => [...row.querySelectorAll('details.tool-run')])
      .find((node) => String(node.querySelector('pre')?.textContent || '').includes('[媒体附卡]'));
    const noteMedia = noteRun ? noteRun.nextElementSibling : null;
    const details = noteRun || null;
    if (details) details.open = true;
    return {
      assistantRows: rows.length,
      toolRuns: mediaRow ? mediaRow.querySelectorAll('.tool-run').length : 0,
      mediaBlocks: mediaRow ? [...mediaRow.querySelectorAll('.tool-media')].length : 0,
      inlineSources: mediaRow ? [...mediaRow.querySelectorAll('.tool-media img[data-large-url]')]
        .map((img) => img.getAttribute('data-large-url')) : [],
      legacyGridImages: legacyRow
        ? legacyRow.querySelectorAll('.message-body > .media-grid img[data-large-url]').length : 0,
      noteSummary: noteRun ? String(noteRun.querySelector('summary')?.textContent || '') : '',
      noteResultTail: noteRun
        ? String(noteRun.querySelector('pre')?.textContent || '').split('\n').slice(-2).join('\n')
        : '',
      noteSuffix: noteRun ? String(noteRun.className) : '',
      noteMediaKind: noteMedia ? String(noteMedia.className) : '',
      noteMediaImages: noteMedia ? noteMedia.querySelectorAll('img[data-large-url]').length : -1,
      // 会话内全部可放大图片（灯箱口径）：就地 + 旧格式 + 指名出卡，均无重复
      conversationImages: document.querySelectorAll('#messages img[data-large-url]').length,
      bottomGrids: mediaRow ? [...mediaRow.querySelectorAll('.message-body > .media-grid')].length : 0,
    };
  });
}

(async () => {
  const list = await apiJson('/api/conversations');
  const conversation = (list.conversations || []).find((item) => String(item.title || '').includes('媒体内嵌'));
  check('已播种「媒体内嵌冒烟」会话', Boolean(conversation), JSON.stringify(list).slice(0, 160));
  if (!conversation) process.exit(1);

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const viewport of VIEWPORTS) {
      const tag = `${viewport.name}@${viewport.width}px`;
      console.log(`--- ${tag} ---`);
      const { page, errors, notFound } = await openMediaConversation(browser, viewport, conversation);
      const snapshotData = await snapshot(page);
      const truncatedTexts = await page.evaluate(
        () => [...document.querySelectorAll('.media-truncated')].map((el) => String(el.textContent || '')),
      );

      check(`${tag} 助手消息包含就地媒体行`, Boolean(snapshotData.toolRuns >= 2), String(snapshotData.toolRuns));
      check(`${tag} 工具块与媒体块按工具各一个`, snapshotData.mediaBlocks >= 2, String(snapshotData.mediaBlocks));
      check(`${tag} 就地渲染 3 张图（生成段）`, snapshotData.inlineSources.length === 3, JSON.stringify(snapshotData.inlineSources));
      check(`${tag} 截断提示渲染（共 25 个媒体，仅显示前 1 个）`,
        truncatedTexts.some((text) => text.includes('共 25 个媒体，仅显示前 1 个')),
        JSON.stringify(truncatedTexts));
      check(`${tag} 新消息末尾网格为空（不重复显示）`, snapshotData.bottomGrids === 0, String(snapshotData.bottomGrids));
      check(`${tag} 旧格式消息仍走末尾网格`, snapshotData.legacyGridImages === 1, String(snapshotData.legacyGridImages));

      check(`${tag} show_media 工具块存在`, snapshotData.noteSummary.includes('show_media'), snapshotData.noteSummary);
      check(`${tag} 工具结果文本含「已附媒体卡」短注`, snapshotData.noteResultTail.includes(NOTE_LINE),
        JSON.stringify(snapshotData.noteResultTail));
      check(`${tag} 指名出卡的来源恰好 1 张卡（无重复）`, snapshotData.noteMediaImages === 1, String(snapshotData.noteMediaImages));
      check(`${tag} 会话内图片总数=6（就地3+旧格式1+指名出卡1+gif附件）`, snapshotData.conversationImages === 6, String(snapshotData.conversationImages));

      check(`${tag} 零 pageerror / console.error`, errors.length === 0, errors.slice(0, 3).join(' | '));
      check(`${tag} 零 404 资源`, notFound.length === 0, notFound.slice(0, 3).join(' | '));
      await page.close();
    }

    // 灯箱（桌面视口）：就地 3 + 旧格式 1 + 指名出卡 1 + gif 附件 = 6。
    // 消息区虚拟重绘：长会话里首图不在窗口内（lazy 未加载、盒子为 0），
    // 直接点最底部那张指名出卡缩略图（必然在窗口内）。
    {
      const { page } = await openMediaConversation(browser, VIEWPORTS[0], conversation);
      await page.waitForSelector('#messages .tool-media img[data-large-url]', { timeout: 20000, state: 'attached' });
      await page.evaluate(() => {
        const images = [...document.querySelectorAll('#messages .tool-media img[data-large-url]')];
        images[images.length - 1]?.scrollIntoView({ block: 'center' });
      });
      await page.waitForTimeout(600);
      await page.click('#messages .tool-media img[alt="seed_shown.png"]');
      await page.waitForTimeout(400);
      const box = await page.evaluate(() => {
        const el = document.querySelector('#imageLightbox');
        const counter = document.querySelector('#imageLightboxCounter');
        return { open: el ? el.hidden === false : false, counter: counter ? counter.textContent : '' };
      });
      check('点击就地缩略图打开灯箱', box.open, JSON.stringify(box));
      check('灯箱打开且计数 6 / 6（会话内全部可放大图片；点击的是最末那张指名出卡）',
        box.counter === '6 / 6', JSON.stringify(box));
      await page.keyboard.press('Escape');
      await page.close();
    }
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`媒体内嵌冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
