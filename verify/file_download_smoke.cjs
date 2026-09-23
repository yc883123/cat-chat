// 「把文件/图片保存到本地」前端入口守门（静态 public/ 服务，无需后端）——§九.135 第 3 项。
//
// 缺陷形态：前端**全局没有任何下载按钮**——文件面板工具栏只有「编辑」，图片灯箱只有缩放/翻页，
// 消息里的非媒体 chip 只有 `target="_blank"` 预览。手机上「下载已生成/修改的文件」整条路不通
// （后端不发 Content-Disposition 只是另一半，见 tests/test_file_download.py）。
//
// 本冒烟钉的是**前端这一半**：按钮存在、点下去真的发起一个带 `download=1` 的同源下载、
// URL 指向正确的文件、文件名从 URL 里解出来（中文保真）、编辑态不与「保存/取消」挤在一起。
//
// 后半段（D 段，⑫~⑱）是用户实测反馈「电脑上点下载这张图没用」之后补的：桌面是 WebView2，
// `<a download>` + attachment 的导航**不会**弹保存框（要宿主处理 DownloadStarting，我们没接），
// 所以有 pywebview 桥时两条「下载」都必须改走原生「另存为」JsApi。D 段单独开一个 context
// 塞假桥——在同一个页面里塞会把 A/B 段的 `<a download>` 断言一起改道。
//
// 为什么要把 `<a>.click()` 截下来而不是真下载：真下载会把产物落到磁盘、还依赖真实文件存在
// （静态服务没有 /file/raw），断言会变得又慢又脆。截下来的只是**导航动作**，
// 「URL 对不对 / 点没点」两条关键事实照样被钉住；响应头那一半由 tests/test_file_download.py 覆盖。
//
// 用法：$env:NODE_PATH="<node_modules>"; node verify\file_download_smoke.cjs
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.NAIBA_DOWNLOAD_SMOKE_PORT || 8818);
const BASE = `http://127.0.0.1:${PORT}`;
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const TEXT_PATH = 'D:\\ws\\生成结果.md';
const IMAGE_PATH = 'D:\\ws\\cover_v3.png';

const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
};
// 空/畸形 href 不该把整条脚本掀翻（`new URL('')` 会抛），解析失败就交回一份空参数表。
const urlParams = (href) => {
  try { return new URL(href).searchParams; } catch (_error) { return new URLSearchParams(); }
};

function waitForServer(deadline = 15) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const once = () => {
      fetch(`${BASE}/index.html`).then((r) => {
        if (r.ok) resolve(); else throw new Error('not ok');
      }).catch(() => {
        if (Date.now() - start > deadline * 1000) reject(new Error('server timeout'));
        else setTimeout(once, 300);
      });
    };
    once();
  });
}

(async () => {
  const server = spawn(PY, ['-m', 'http.server', String(PORT), '--directory', path.join(ROOT, 'public')], {
    cwd: ROOT, windowsHide: true,
  });
  server.stderr.on('data', (d) => console.error(String(d).trim()));
  let code = 1;
  try {
    await waitForServer();
    const browser = await chromium.launch({ channel: 'msedge', headless: true });
    const context = await browser.newContext({ viewport: { width: 360, height: 780 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true });
    const page = await context.newPage();
    page.setDefaultTimeout(10000); // 断言失败时不要卡满 30s
    const errors = [];
    page.on('pageerror', (e) => { if (!/^HTTP 404$/.test(e.message)) errors.push(`pageerror: ${e.message}`); });
    page.on('console', (msg) => {
      if (msg.type() === 'error' && !msg.text().includes('404') && !msg.text().includes('Failed to load resource')) {
        errors.push(`console.error: ${msg.text()}`);
      }
    });
    // 把「下载动作」记在窗口上：只拦带 download 属性或带 download=1 的锚点，其余点击照旧。
    await page.addInitScript(() => {
      window.__downloads = [];
      const original = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function patchedClick() {
        if (this.hasAttribute('download') || /[?&]download=1(?:&|$)/.test(this.href)) {
          window.__downloads.push({ href: this.href, name: this.getAttribute('download') || '' });
          return undefined;
        }
        return original.call(this);
      };
    });
    await page.addInitScript(() => { try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) {} });
    await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
    await page.waitForSelector('#messageInput', { timeout: 15000 });
    await page.waitForTimeout(400);
    await page.evaluate(async () => {
      // eslint-disable-next-line no-undef
      window.__m = {
        core: await import('./js/01-core.js'),
        filePanel: await import('./js/14-file-panel.js'),
        media: await import('./js/03-media.js'),
      };
      document.querySelectorAll('dialog[open]').forEach((d) => d.close());
    });

    // ── A. 文件面板工具栏的「下载」
    await page.evaluate((textPath) => {
      const fp = window.__m.filePanel;
      fp.filePanelState.tabs = [{
        key: 'smoke-tab',
        name: '生成结果.md',
        raw: textPath,
        info: { path: textPath, name: '生成结果.md', kind: 'text', savable: true, truncated: false, content: '正文', size: 6, mtime: 1700000000000 },
      }];
      fp.filePanelState.activeKey = 'smoke-tab';
      fp.filePanelState.open = true;
      fp.renderFilePanel();
    }, TEXT_PATH);
    check('① 文件面板工具栏里出现「下载」按钮',
      (await page.locator('[data-file-download]').count()) === 1,
      `count=${await page.locator('[data-file-download]').count()}`);
    // ① 没过就别硬点：点不到会抛异常、把 ②–⑪ 一起带走，读日志的人会以为是十个问题
    if (await page.locator('[data-file-download]').count() === 1) {
      await page.click('[data-file-download]');
      await page.waitForTimeout(150);
    }
    let downloads = await page.evaluate(() => window.__downloads);
    check('② 点「下载」真的发起了一次下载动作（不是空按钮）', downloads.length === 1, JSON.stringify(downloads));
    let href = downloads[0]?.href || '';
    // 空/畸形 href 不该把整条脚本掀翻（new URL('') 会抛），后面几条照样要出结论。
    const params = urlParams(href);
    check('③ 下载 URL 指向本会话的 /file/raw 且带 download=1',
      href.includes('/api/conversations/') && href.includes('/file/raw') && /[?&]download=1(&|$)/.test(href),
      href);
    check('④ URL 里的 path 就是当前标签的那个文件（编码正确、可逆）',
      params.get('path') === TEXT_PATH,
      JSON.stringify(params.get('path')));
    check('⑤ 文件名从 URL 里解出来交给 <a download>（中文保真，作为 Content-Disposition 的兜底）',
      downloads[0]?.name === '生成结果.md', JSON.stringify(downloads[0]?.name));

    // 编辑态：不许和「保存 / 取消」挤在一起（编辑中的文件还没有落盘内容可下载）
    await page.evaluate(() => {
      const fp = window.__m.filePanel;
      fp.filePanelState.tabs[0].editing = true;
      fp.filePanelState.tabs[0].draft = '正文';
      fp.renderFilePanel();
    });
    const editingBtn = await page.evaluate(() => ({
      download: document.querySelectorAll('[data-file-download]').length,
      save: document.querySelectorAll('[data-file-save]').length,
    }));
    check('⑥ 编辑态：工具栏是「取消 / 保存」，不出现「下载」',
      editingBtn.download === 0 && editingBtn.save === 1, JSON.stringify(editingBtn));

    // ── B. 灯箱「下载」
    // 静态服务没有 /file/raw ⇒ 大图必然加载失败，而「大图不存在」分支会主动 closeImageLightbox()
    // （产品行为，不是缺陷）。这里在 openImageLightbox() 返回后立刻摘掉 onerror——错误事件是异步的，
    // 同一轮同步代码里摘掉即可，且**不触及盒子的开关逻辑**，⑦ 断言的仍然是「盒子真的开了」。
    const lightboxOpened = await page.evaluate((imagePath) => {
      const url = `/api/file?token=t&path=${encodeURIComponent(imagePath)}`;
      window.__m.media.openImageLightbox(url, null);
      const img = document.querySelector('#imageLightboxImg');
      if (img) img.onerror = null;
      const box = document.querySelector('#imageLightbox');
      return { hidden: box.hidden, ariaHidden: box.getAttribute('aria-hidden') };
    }, IMAGE_PATH);
    await page.waitForTimeout(150);
    const lightboxDownloads = await page.locator('#imageLightboxDownload').count();
    check('⑦ 灯箱里出现「下载」按钮（此前只有关闭/翻页/缩放，没有任何保存入口）',
      lightboxDownloads === 1
      && (await page.evaluate(() => document.querySelector('#imageLightbox').hidden)) === false,
      JSON.stringify({ buttonCount: lightboxDownloads, ...lightboxOpened }));
    await page.evaluate(() => { window.__downloads = []; });
    // ⑦ 没过就别硬点（点不到会抛异常、把 ⑩⑪ 一起带走，读日志的人会以为是三个问题）
    if (lightboxDownloads === 1) {
      await page.click('#imageLightboxDownload');
      await page.waitForTimeout(150);
    }
    downloads = await page.evaluate(() => window.__downloads);
    href = downloads[0]?.href || '';
    check('⑧ 点灯箱「下载」发起同源下载，`download=1` 被加上且原参数不丢（token / path 都在）',
      downloads.length === 1 && /[?&]download=1(&|$)/.test(href)
      && urlParams(href).get('path') === IMAGE_PATH
      && urlParams(href).get('token') === 't',
      href);
    check('⑨ 灯箱下载的文件名取自图片路径（cover_v3.png）',
      downloads[0]?.name === 'cover_v3.png', JSON.stringify(downloads[0]?.name));

    // ── C. 边界：外链不加参数（跨源 URL 的 <a download> 本就被浏览器忽略，加参数只会有副作用）
    const boundary = await page.evaluate(() => {
      window.__downloads = [];
      window.__m.core.triggerDownload('https://other.example/pic.png');
      window.__m.core.triggerDownload('');
      return window.__downloads;
    });
    check('⑩ 外链原样交给浏览器（不加 download=1），空地址不产生任何动作',
      boundary.length === 1 && !boundary[0].href.includes('download=1') && boundary[0].href === 'https://other.example/pic.png',
      JSON.stringify(boundary));

    check('⑪ 零 pageerror / 零 console.error（404 噪音除外）', errors.length === 0, errors.join(' | '));
    await context.close();

    // ── D. 桌面（有 pywebview 桥）：<a download> 在 WebView2 里点了没反应，
    //     必须改走原生「另存为」JsApi。用户实测原话「电脑上点下载这张图没用」就是这一段。
    //     单独开一个 context 塞假桥——在同一个页面里塞会把 A/B 段的 `<a download>` 断言一起改道。
    const deskCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const desk = await deskCtx.newPage();
    desk.setDefaultTimeout(10000);
    const deskErrors = [];
    desk.on('pageerror', (e) => { if (!/^HTTP 404$/.test(e.message)) deskErrors.push(`pageerror: ${e.message}`); });
    desk.on('console', (msg) => {
      if (msg.type() === 'error' && !msg.text().includes('404') && !msg.text().includes('Failed to load resource')) {
        deskErrors.push(`console.error: ${msg.text()}`);
      }
    });
    await desk.addInitScript(() => {
      window.__downloads = [];
      const original = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function patchedClick() {
        if (this.hasAttribute('download') || /[?&]download=1(?:&|$)/.test(this.href)) {
          window.__downloads.push({ href: this.href });
          return undefined;
        }
        return original.call(this);
      };
      // 假桥：任何方法都回 {ok:true}，只把 naibaSaveFileAs 的入参记下来。
      // 用 Proxy 而不是逐个列方法——init 期还有别的桥调用，漏一个就会 TypeError 干扰断言。
      window.__saveAs = [];
      window.pywebview = {
        platform: 'edgechromium',
        api: new Proxy({}, {
          get(_target, prop) {
            return (...args) => {
              if (prop === 'naibaSaveFileAs') window.__saveAs.push({ path: args[0], name: args[1] });
              return Promise.resolve({ ok: true });
            };
          },
        }),
      };
    });
    await desk.addInitScript(() => { try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) {} });
    await desk.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
    await desk.waitForSelector('#messageInput', { timeout: 15000 });
    await desk.waitForTimeout(400);
    await desk.evaluate(async () => {
      window.__m = {
        core: await import('./js/01-core.js'),
        filePanel: await import('./js/14-file-panel.js'),
        media: await import('./js/03-media.js'),
      };
      document.querySelectorAll('dialog[open]').forEach((d) => d.close());
    });
    await desk.evaluate((textPath) => {
      const fp = window.__m.filePanel;
      fp.filePanelState.tabs = [{
        key: 'desk-tab',
        name: '生成结果.md',
        raw: textPath,
        info: { path: textPath, name: '生成结果.md', kind: 'text', savable: true, truncated: false, content: '正文', size: 6, mtime: 1700000000000 },
      }];
      fp.filePanelState.activeKey = 'desk-tab';
      fp.filePanelState.open = true;
      fp.renderFilePanel();
    }, TEXT_PATH);
    await desk.evaluate(() => { window.__saveAs = []; window.__downloads = []; });
    await desk.click('[data-file-download]');
    await desk.waitForTimeout(150);
    const deskSaveAs = await desk.evaluate(() => window.__saveAs);
    const deskDownloads = await desk.evaluate(() => window.__downloads);
    check('⑫ 桌面点「下载」→ 走原生「另存为」，把该文件路径交给 JsApi（WebView2 里 <a download> 点了没反应）',
      deskSaveAs.length === 1 && deskSaveAs[0].path === TEXT_PATH && deskSaveAs[0].name === '生成结果.md',
      JSON.stringify(deskSaveAs));
    check('⑬ 桌面不再走 <a download>（那条路在 WebView2 里是"点了等于没反应"）',
      deskDownloads.length === 0, JSON.stringify(deskDownloads));

    // 灯箱：用户就是在这一刻发现「没用」的（截图里右上角那个 ↓）。
    // 静态服务下没有 /api/file ⇒ 大图 error 会触发产品自身的「大图不存在」分支主动关灯箱、
    // 按钮随之不可点（section A ⑦ 同款坑）：开完灯箱**在同一轮同步代码里**把 onerror 摘掉。
    await desk.evaluate((imagePath) => {
      window.__saveAs = [];
      window.__downloads = [];
      window.__m.media.openImageLightbox(`/api/file?path=${encodeURIComponent(imagePath)}`, null);
      const img = document.querySelector('#imageLightboxImg');
      if (img) img.onerror = null;
    }, IMAGE_PATH);
    await desk.waitForTimeout(150);
    const lightboxReady = await desk.evaluate(() => {
      const box = document.querySelector('#imageLightbox');
      return Boolean(box && !box.hidden && document.querySelector('#imageLightboxDownload'));
    });
    check('⑭-0 前提成立：灯箱真的开着、下载按钮可点（否则下面那条是空断言）', lightboxReady, String(lightboxReady));
    if (lightboxReady) {
      await desk.click('#imageLightboxDownload');
      await desk.waitForTimeout(150);
    }
    const lbSaveAs = await desk.evaluate(() => window.__saveAs);
    const lbDownloads = await desk.evaluate(() => window.__downloads);
    check('⑭ 桌面点灯箱「下载」→ 同样走原生「另存为」（这是用户实测"点下载没用"的那一颗按钮）',
      lightboxReady && lbSaveAs.length === 1 && lbSaveAs[0].path === IMAGE_PATH,
      JSON.stringify({ lightboxReady, lbSaveAs }));
    check('⑮ 灯箱下载在桌面上也不再产生 <a download>', lbDownloads.length === 0, JSON.stringify(lbDownloads));

    // 用户在原生保存框里按「取消」不是失败，不能弹「另存为失败」吓人。
    // 注意 toast 是**异步**写的（Promise 回调里），读一次当前值抓不到 ⇒ 装一个"曾经出现过什么
    // toast"的记录器；并且必须先做一组**前提断言**证明记录器真的在工作（真失败要被抓到），
    // 否则下面那条「取消不报错」在记录器失灵时会变成永远通过的空断言。
    await desk.evaluate(() => {
      window.__toasts = [];
      const box = document.querySelector('#toast');
      const push = () => { const t = (box?.textContent || '').trim(); if (t) window.__toasts.push(t); };
      if (box) new MutationObserver(push).observe(box, { childList: true, characterData: true, subtree: true });
    });
    const setSaveResult = (result) => desk.evaluate((r) => {
      window.pywebview.api = new Proxy({}, {
        get(_t, prop) { return () => Promise.resolve(prop === 'naibaSaveFileAs' ? r : { ok: true }); },
      });
    }, result);

    await setSaveResult({ ok: false, error: '磁盘已满（正控）' });
    await desk.evaluate((imagePath) => {
      window.__m.media.saveLocalFile(`/api/file?path=${encodeURIComponent(imagePath)}`, 'cover_v3.png');
    }, IMAGE_PATH);
    await desk.waitForTimeout(250);
    const failedToasts = await desk.evaluate(() => window.__toasts);
    check('⑯ 前提成立：真失败时确实弹了错误提示（证明下面那条「取消不报错」不是空断言）',
      failedToasts.some((t) => t.includes('磁盘已满')), JSON.stringify(failedToasts));

    await desk.evaluate(() => { window.__toasts = []; });
    await setSaveResult({ ok: false, cancelled: true });
    await desk.evaluate((imagePath) => {
      window.__m.media.saveLocalFile(`/api/file?path=${encodeURIComponent(imagePath)}`, 'cover_v3.png');
    }, IMAGE_PATH);
    await desk.waitForTimeout(250);
    const cancelToasts = await desk.evaluate(() => window.__toasts);
    check('⑰ 用户点「取消」不算失败（不得弹「另存为失败」）',
      cancelToasts.length === 0, JSON.stringify(cancelToasts));
    check('⑱ 桌面段零 pageerror / 零 console.error（404 噪音除外）', deskErrors.length === 0, deskErrors.join(' | '));

    await browser.close();
    code = failures.length ? 1 : 0;
    console.log(failures.length ? `\n${failures.length} 条断言失败：${failures.join(' / ')}` : '\n全部通过');
  } catch (error) {
    console.error('异常：', error.message);
    code = 1;
  } finally {
    server.kill();
    setTimeout(() => process.exit(code), 200);
  }
})();
