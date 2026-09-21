// 折叠文件夹拖入冒烟（由 verify/folder_drop_smoke.py 编排：隔离源码实例 + 真后端 + 无头 Edge）。
//
// 为什么必须真后端（§九.92）：文件夹 chip、索引清单、工作区外确认框全都要真 HTTP——
// 静态 public/ 服务下 /api/files/folder-index 一律 404，而"点一下没反应"和"被拦下来了"
// 在截图上长得一模一样，断言会全绿却什么都没验。
//
// 覆盖：
//   ① 气泡里的文件夹索引：默认只列前 10 项 + 「另有 N 项未显示」，点开才铺完整清单（含截断自述）；
//   ② 拖入（桌面壳交回绝对路径的同一条 JS 通路）只产出**一个**占位 chip，不铺缩略图；
//   ③ 文件面板目录行的「＋」：第二条入口（也是手机上唯一可用的那条）；
//   ④ 工作区外目录：先弹确认框 → 「不允许」不读盘、「允许」才建 chip 且不再问第二次；
//   ⑤ 零页面错误、零未解释的 4xx。
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8812';
const TITLE = process.env.NAIBA_FOLDER_CONV_TITLE || '文件夹气泡冒烟';
const INSIDE = process.env.NAIBA_FOLDER_INSIDE || '';
const OUTSIDE = process.env.NAIBA_FOLDER_OUTSIDE || '';
const PICK_NAME = process.env.NAIBA_FOLDER_PICK || '素材夹';
const SHOTS = process.env.NAIBA_SMOKE_SHOTS || path.join(__dirname);
const shot = (name) => path.join(SHOTS, `folder_drop_smoke_${name}.png`);

const failures = [];

function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 940 } });
  const pageErrors = [];
  const httpErrors = [];
  let modelsNoise = 0;    // 占位供应商 example.invalid：拦掉，单独计数
  let confirmNoise = 0;   // /api/files/folder-index 的 403 needs_confirm：**预期内**的 4xx，单独计数
  let expectedConsole = 0; // 上面那几次 403，浏览器控制台必然各报一条 Failed to load resource
  const folderIndexLog = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    // 预期内的 403：计数而不是忽略——最后要求「控制台 403 条数 == 预期 403 条数」，
    // 少一条说明确认链路没走到，多一条说明有别的东西在失败。
    if (/Failed to load resource/.test(text) && /403/.test(text)) { expectedConsole += 1; return; }
    pageErrors.push(`console.error: ${text}`);
  });
  page.on('response', (response) => {
    if (response.url().includes('/api/files/folder-index')) {
      folderIndexLog.push(`${response.status()} ${decodeURIComponent(response.url())}`);
    }
    if (response.status() < 400) return;
    const line = `${response.status()} ${response.url()}`;
    if (line.includes('/api/files/folder-index')) confirmNoise += 1;
    else httpErrors.push(line);
  });

  // 轮询等待：失败时能带出诊断，而不是抛异常把后续断言全跳过。
  const waitFor = async (predicate, timeout = 15000, interval = 150) => {
    const deadline = Date.now() + timeout;
    for (;;) {
      if (await predicate()) return true;
      if (Date.now() > deadline) return false;
      await page.waitForTimeout(interval);
    }
  };
  const diag = () => page.evaluate(() => {
    const dialog = document.querySelector('#folderAccessDialog');
    return {
      dialogExists: Boolean(dialog),
      dialogOpen: Boolean(dialog?.open),
      toast: (document.querySelector('#toast')?.textContent || '').trim().slice(0, 120),
      chips: document.querySelectorAll('#pendingFiles .pending-item').length,
      popupOpen: document.querySelector('#filePopup')?.hidden === false,
    };
  });

  const chips = () => page.evaluate(() => [...document.querySelectorAll('#pendingFiles .pending-item')].map((row) => ({
    folder: row.classList.contains('is-folder'),
    name: row.querySelector('.pending-name')?.textContent || '',
    status: row.querySelector('.pending-status')?.textContent || '',
    thumbs: row.querySelectorAll('img.pending-thumb').length,
    icon: Boolean(row.querySelector('.pending-thumb-folder svg')),
  })));
  const dialogOpen = () => page.evaluate(() => Boolean(document.querySelector('#folderAccessDialog')?.open));
  const dropPaths = (paths) => page.evaluate((list) => { window.naibaHandleDroppedFolders(list); }, paths);

  try {
    // 隔离实例首启必弹引导层（top layer 会拦住所有点击）——冒烟只看它会话内的行为，直接标记已读。
    await page.addInitScript(() => { try { localStorage.naibaOnboardingDismissed = '1'; } catch (_) { /* 无存储 */ } });
    // 占位供应商的模型列表请求必然失败：拦掉并计数（噪音不能混进"未解释的 4xx"）。
    await page.route('**/api/providers/models*', (route) => {
      modelsNoise += 1;
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"models":[]}' });
    });

    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${TITLE}") .conversation-open`);
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 20000 });
    await page.waitForTimeout(800);

    // ==================================================== ① 气泡：前 10 项 + 可折叠
    const bubble = await page.evaluate(() => {
      const box = document.querySelector('#messages .folder-index');
      if (!box) return null;
      const preview = box.querySelector('.folder-index-preview');
      const list = box.querySelector('.folder-index-list');
      const items = (scope) => scope ? [...scope.querySelectorAll('li')] : [];
      // 折叠态是否真的"看不见"：closed <details> 的非 summary 内容（Chromium 用
      // content-visibility 实现）在 checkVisibility() 下为 false；老实现回退到 offsetHeight。
      const listVisible = list
        ? (typeof list.checkVisibility === 'function' ? list.checkVisibility() : list.offsetHeight > 0)
        : false;
      return {
        head: box.querySelector('.folder-index-head')?.textContent || '',
        open: box.open,
        previewTotal: items(preview).length,
        previewEntries: items(preview).filter((li) => !li.classList.contains('folder-index-more')).length,
        previewHint: preview?.querySelector('.folder-index-more')?.textContent || '',
        listEntries: items(list).filter((li) => !li.classList.contains('folder-index-more')).length,
        listHint: list?.querySelector('.folder-index-more')?.textContent || '',
        path: box.querySelector('.folder-index-path')?.textContent || '',
        listVisible,
        listHeight: list?.offsetHeight ?? -1,
        hasDetails: box.tagName === 'DETAILS',
      };
    });
    check('气泡里有文件夹索引块（<details>）', Boolean(bubble && bubble.hasDetails), JSON.stringify(bubble));
    check('标题行写明总数与图片数', Boolean(bubble && /共 \d+ 项（含 \d+ 图）/.test(bubble.head)), bubble?.head);
    check('默认只列前 10 项', Boolean(bubble && bubble.previewEntries === 10), JSON.stringify(bubble));
    check('第 11 项起折叠（另有 N 项未显示）',
      Boolean(bubble && /另有 \d+ 项未显示/.test(bubble.previewHint)), bubble?.previewHint);
    check('默认不展开完整清单', Boolean(bubble && bubble.open === false && bubble.listVisible === false),
      JSON.stringify({ open: bubble?.open, visible: bubble?.listVisible, height: bubble?.listHeight }));
    check('绝对路径单独一行（模型与用户都按它拼相对路径）',
      Boolean(bubble && bubble.path === process.env.NAIBA_FOLDER_SEED), bubble?.path);
    await page.screenshot({ path: shot('1_bubble_collapsed') });

    await page.click('#messages .folder-index > summary');
    await page.waitForTimeout(300);
    const expanded = await page.evaluate(() => {
      const box = document.querySelector('#messages .folder-index');
      const list = box.querySelector('.folder-index-list');
      return {
        open: box.open,
        visible: Boolean(list) && list.offsetHeight > 0,
        // 展开后「前 10 项」仍在 summary 里，这里数的是 summary 之外的那截（第 11 项起）
        entries: [...list.querySelectorAll('li')].filter((li) => !li.classList.contains('folder-index-more')).length,
        hint: list.querySelector('.folder-index-more')?.textContent || '',
      };
    });
    check('点开后完整清单可见，且逐条铺到第 30 项（10 预览 + 20 展开）',
      expanded.visible && expanded.entries === 20, JSON.stringify(expanded));
    check('清单自述截断（别把不全的清单当成目录全部）',
      /仅列出前 30 项，另有 \d+ 项未列出/.test(expanded.hint), expanded.hint);
    await page.screenshot({ path: shot('2_bubble_expanded') });

    // ==================================================== ② 拖入 = 一个占位，不铺缩略图
    await dropPaths([INSIDE]);
    await page.waitForSelector('#pendingFiles .pending-item.is-folder', { timeout: 20000 });
    let state = await chips();
    check('拖入文件夹只产生 1 个待发送条目', state.length === 1 && state[0].folder === true, JSON.stringify(state));
    check('占位图标是文件夹图标、不是缩略图', state[0]?.icon === true && state[0]?.thumbs === 0, JSON.stringify(state));
    check('占位显示统计（N 项 · 含 M 图）', /\d+ 项 · 含 \d+ 图/.test(state[0]?.status || ''), state[0]?.status);
    await page.screenshot({ path: shot('3_chip') });

    // 同一个目录再拖一次：去重，不叠第二个 chip
    await dropPaths([INSIDE]);
    await page.waitForTimeout(600);
    state = await chips();
    check('重复拖入同一目录不叠条目', state.length === 1, JSON.stringify(state));

    // ==================================================== ③ 文件面板目录行的「＋」
    await page.click('#pendingFiles .pending-item.is-folder .pending-remove');
    await page.waitForTimeout(300);
    check('移除按钮清掉文件夹条目', (await chips()).length === 0, JSON.stringify(await chips()));

    await page.fill('#messageInput', '@');
    await page.waitForSelector('#filePopup:not([hidden]) .file-popup-item', { timeout: 15000 });
    const plusVisible = await page.evaluate((name) => {
      const row = [...document.querySelectorAll('#filePopup .file-popup-item')]
        .find((el) => (el.querySelector('.file-popup-name')?.textContent || '') === name);
      return Boolean(row?.querySelector('.file-popup-add'));
    }, PICK_NAME);
    check('目录行上有「＋」按钮', plusVisible === true, `找的是「${PICK_NAME}」`);
    await page.click(`#filePopup .file-popup-item:has-text("${PICK_NAME}") .file-popup-add`);
    await page.waitForSelector('#pendingFiles .pending-item.is-folder', { timeout: 20000 });
    state = await chips();
    check('「＋」加出一个文件夹条目', state.length === 1 && state[0].folder === true, JSON.stringify(state));
    check('「＋」不该顺带改动输入框里的 @ 引用',
      (await page.inputValue('#messageInput')) === '@', await page.inputValue('#messageInput'));
    await page.keyboard.press('Escape');
    await page.click('#pendingFiles .pending-item.is-folder .pending-remove');
    await page.waitForTimeout(300);

    // ==================================================== ④ 工作区外：先问一次
    await dropPaths([OUTSIDE]);
    const asked = await waitFor(() => dialogOpen());
    check('工作区外目录弹确认框', asked, JSON.stringify({ diag: await diag(), requests: folderIndexLog }));
    if (asked) {
      check('确认框里写明具体路径（用户能自己核对）',
        (await page.textContent('#folderAccessPath')) === OUTSIDE, await page.textContent('#folderAccessPath'));
    }
    await page.screenshot({ path: shot('4_confirm') });
    if (asked) {
      await page.click('#folderAccessDeny');
      await page.waitForTimeout(800);
      check('「不允许」之后不建条目', (await chips()).length === 0, JSON.stringify(await chips()));

      await dropPaths([OUTSIDE]);
      const askedAgain = await waitFor(() => dialogOpen());
      check('被拒之后重拖仍会问（拒绝不写确认记录）', askedAgain, JSON.stringify(await diag()));
      if (askedAgain) {
        await page.click('#folderAccessAllow');
        await page.waitForSelector('#pendingFiles .pending-item.is-folder', { timeout: 20000 });
        state = await chips();
        check('「允许」之后建出条目（只读清单，不上传内容）',
          state.length === 1 && state[0].folder === true, JSON.stringify(state));

        // 同会话再拖一次：确认过就不再问（口径二：按会话记住）
        await dropPaths([OUTSIDE]);
        await page.waitForTimeout(1500);
        check('同会话内不再重复询问', (await dialogOpen()) === false && (await chips()).length === 1,
          JSON.stringify({ diag: await diag(), requests: folderIndexLog }));
      }
    }

    // ==================================================== ⑤ 噪音门
    check('零页面错误 / console.error（预期内的 403 网络报错除外）',
      pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
    check('零未解释的 4xx / 5xx', httpErrors.length === 0, httpErrors.slice(0, 3).join(' | '));
    // 403 真的发生过：否则"确认框弹出来了"可能只是前端自己编的
    check('工作区外确实被后端 403 needs_confirm 拦过', confirmNoise >= 2,
      `folder-index 4xx=${confirmNoise} models=${modelsNoise}`);
    check('控制台 403 条数与预期一致（确认链路既没漏也没多）',
      expectedConsole === confirmNoise, `console=${expectedConsole} http=${confirmNoise}`);
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
