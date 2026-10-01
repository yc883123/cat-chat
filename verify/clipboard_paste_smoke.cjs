// 输入区右键「粘贴」粘图片 / 文件的**真浏览器**端到端冒烟（§九.149）。
//
// 运行：
//   $env:NODE_PATH="C:\Users\ylxia\node_modules"
//   python -m http.server 8795 --directory public      # 只需静态服务
//   node verify\clipboard_paste_smoke.cjs
//
// 覆盖：注入假原生桥（`window.pywebview.api.naibaClipboardPayload`）→
//       ① 图片：菜单文案变「粘贴图片并上传」→ 点击后真的把 File 交给上传管线（`POST /api/uploads` 命中 1 次）
//          且待发送列表出现带缩略图的条目；
//       ② 文件（CF_HDROP 形态）：文案变「粘贴 2 个文件」→ 点击后进**路径附件**（带 path，不发上传请求）；
//       ③ 重复粘贴同一批文件：不产生重复条目；
//       ④ 只有文本：文案仍是「粘贴」，且**不产生任何待发送条目**（交回原有 readText 通道）；
//       ⑤ 无桥（纯浏览器/手机形态）：驱动为空，同样不产生条目（手机口径不变）。
//
// 为什么直接调 `showTextContextMenu` / `runTextContextAction` 而不是在页面上真右键：
// 本仓已有 `context_menu_smoke.cjs` 覆盖"右键事件 → 菜单弹出"那一段（跑在真源码 server 上）；
// 这条要钉的是**本次新增的那段**（探测文案 → 分流 → 进列表/上传），用静态服务即可，不依赖后端。
// 因此覆盖范围是刻意收窄的：它证明"前端接线在真实浏览器里通"，不证明"后端存盘成功"。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8795';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

// 1×1 透明 PNG：足够让 `new File([bytes], name, {type:'image/png'})` 成立、并让 <img> 解码成功。
const PNG_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==';
const FILES_PAYLOAD = {
  ok: true,
  kind: 'files',
  paths: ['D:\\smoke\\中文图片.png', 'D:\\smoke\\第二个文件.txt'],
  names: ['中文图片.png', '第二个文件.txt'],
  sizes: [2048, 12],
};

async function pasteMenuLabel(page) {
  return page.evaluate(async () => {
    const core = await import('/js/01-core.js');
    const input = document.querySelector('#messageInput');
    input.focus();
    core.showTextContextMenu({ clientX: 40, clientY: 40 }, '', 'edit', input);
    await new Promise((resolve) => setTimeout(resolve, 260));   // 等异步回填文案
    const button = document.querySelector('#textContextMenu button[data-context-action="paste"]');
    return button ? button.textContent.trim() : '(没有粘贴项)';
  });
}

async function clickPaste(page) {
  await page.evaluate(async () => {
    const core = await import('/js/01-core.js');
    await core.runTextContextAction('paste');
  });
  await page.waitForTimeout(400);   // 等上传 chips 渲染
}

async function pendingNames(page) {
  return page.evaluate(() => [...document.querySelectorAll('#pendingFiles .pending-item')]
    .map((el) => el.querySelector('.pending-name')?.textContent?.trim() || ''));
}

async function pendingPaths(page) {
  return page.evaluate(async () => {
    const core = await import('/js/01-core.js');
    return (core.state.pendingFiles || []).map((item) => String(item.path || ''));
  });
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));

  let uploadCalls = 0;
  await page.route('**/api/uploads', (route) => {
    uploadCalls += 1;
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ path: '/api/file?path=uploads%2Fsmoke.png', name: '粘贴的图片.png', size: 68 }),
    });
  });
  // 假桥：必须在页面脚本之前就位（模块顶层会读 window.pywebview）
  await page.addInitScript(() => {
    window.__clipPayload = { ok: true, kind: 'empty' };
    window.pywebview = { api: { naibaClipboardPayload: async () => window.__clipPayload } };
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    // 静态服务下没有后端：把会话 id 直接塞好，避免图片分支去走 createConversation()。
    const wired = await page.evaluate(async () => {
      const core = await import('/js/01-core.js');
      const chat = await import('/js/12-chat-input.js');
      core.setClipboardPasteDriver(chat.clipboardPasteDriver);
      core.state.conversationId = 'smoke-conversation';
      return Boolean(chat.clipboardPasteDriver && chat.clipboardPasteDriver.apply);
    });
    check('驱动可注入且具备 apply', wired);

    // ---- ① 图片：文案 + 上传 ----
    await page.evaluate((b64) => {
      window.__clipPayload = { ok: true, kind: 'image', mime: 'image/png', base64: b64, name: '粘贴的图片.png' };
    }, PNG_B64);
    check('① 菜单文案变「粘贴图片并上传」', (await pasteMenuLabel(page)) === '粘贴图片并上传');
    await clickPaste(page);
    check('① 图片进了待发送列表', (await pendingNames(page)).includes('粘贴的图片.png'),
      JSON.stringify(await pendingNames(page)));
    check('① 上传请求真的发出（File 交给了上传管线）', uploadCalls === 1, `uploadCalls=${uploadCalls}`);
    check('① 待发送列表可见且带缩略图', await page.evaluate(() => {
      const box = document.querySelector('#pendingFiles');
      return Boolean(box && !box.hidden && box.querySelector('.pending-thumb'));
    }));

    // ---- ② 文件：文案 + 路径附件（零拷贝，不发上传请求)----
    const uploadsBefore = uploadCalls;
    await page.evaluate((payload) => { window.__clipPayload = payload; }, FILES_PAYLOAD);
    check('② 菜单文案变「粘贴 2 个文件」', (await pasteMenuLabel(page)) === '粘贴 2 个文件');
    await clickPaste(page);
    const names = await pendingNames(page);
    check('② 两个文件名都进了待发送列表',
      names.includes('中文图片.png') && names.includes('第二个文件.txt'), JSON.stringify(names));
    check('② 走的是路径附件（带 path，零拷贝）',
      (await pendingPaths(page)).includes('D:\\smoke\\中文图片.png'), JSON.stringify(await pendingPaths(page)));
    check('② 文件分支不发上传请求（没有多余字节过网）', uploadCalls === uploadsBefore,
      `uploads ${uploadsBefore} -> ${uploadCalls}`);

    // ---- ③ 重复粘贴：不产生重复条目 ----
    await clickPaste(page);
    const duplicated = await page.evaluate(() => {
      const paths = (window.__pendingPathsCache = null, null);
      return document.querySelectorAll('#pendingFiles .pending-item').length;
    });
    check('③ 重复粘贴不产生重复条目', duplicated === 3, `pending 条目数=${duplicated}`);

    // ---- ④ 只有文本：文案不变、不产生条目 ----
    await page.evaluate(() => { window.__clipPayload = { ok: true, kind: 'text' }; });
    check('④ 文本形态文案仍是「粘贴」', (await pasteMenuLabel(page)) === '粘贴');
    const before = duplicated;
    await clickPaste(page);
    check('④ 文本形态不产生待发送条目', (await pendingNames(page)).length === before);

    // ---- ⑤ 无桥（手机/纯浏览器形态）：驱动为空，不产生条目 ----
    await page.evaluate(async () => {
      const core = await import('/js/01-core.js');
      core.setClipboardPasteDriver(null);
    });
    const withoutBridge = await page.evaluate(async () => {
      const core = await import('/js/01-core.js');
      await core.runTextContextAction('paste');
      await new Promise((resolve) => setTimeout(resolve, 200));
      const toastEl = document.querySelector('#toast');
      return {
        count: document.querySelectorAll('#pendingFiles .pending-item').length,
        toast: toastEl ? toastEl.textContent.trim() : '',
      };
    });
    check('⑤ 无桥时不产生条目', withoutBridge.count === before, JSON.stringify(withoutBridge));
    check('⑤ 无桥时仍给可行动提示', /Ctrl\+V|长按/.test(withoutBridge.toast), withoutBridge.toast);

    const fatal = pageErrors.filter((line) => !/Failed to fetch|NetworkError|ERR_/i.test(line));
    check('零非网络类页面异常', fatal.length === 0, fatal.join(' | '));
  } catch (error) {
    check(`冒烟执行未抛异常`, false, error.message);
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\n存在未通过项：${failures.join(' / ')}` : '\n全部通过');
  process.exit(failures.length ? 1 : 0);
})();
