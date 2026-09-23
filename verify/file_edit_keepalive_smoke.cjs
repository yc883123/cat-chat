// 「手机编辑文件」交互守门（静态 public/ 服务，无需后端）——§九.135 第 2 项。
//
// 缺陷形态（两条，本冒烟分别钉住）：
//   A. `renderFilePanel()` 每次都 `body.innerHTML = ...` 整体重建 ⇒ 编辑中的 textarea 被连根拔掉，
//      选区、光标、正显示着的系统「复制 / 粘贴」菜单一起消失 —— 手机上"编辑文件很奇怪"的头号成因。
//      触发点很日常：编辑中再点一次文件 chip（`reloadFileTab` 在草稿未改时会真的重读）、
//      点另一个标签再点回来、保存失败后重绘……每一次都是一次"选区凭空没了"。
//   B. 「保存 / 取消」挂在**顶部工具栏**，手机上键盘一弹就被挤出可视区 ⇒ "改完存不了"。
//      现在放到编辑区脚底（flex 列的最后一条，随面板一起收缩），并过 44px 最小点按尺寸。
//
// 本冒烟先用「缺陷前提成立」的独立断言把两条都立住（编辑态真的进了、textarea 真的在编辑区里），
// 再断言重绘后**还是同一个 DOM 节点**、选区没丢、草稿没丢、工具栏却真的更新了
// （否则"整块跳过重绘"也能让上面几条全绿）。
//
// 用法：$env:NODE_PATH="<node_modules>"; node verify\file_edit_keepalive_smoke.cjs
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.NAIBA_FILE_EDIT_SMOKE_PORT || 8821);
const BASE = `http://127.0.0.1:${PORT}`;
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const TEXT_PATH = 'D:\\ws\\生成结果.md';
const DRAFT = '一二三四五六七八';

const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
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

// 进编辑态：直接摆好 tab 状态再 startFileEdit（与"点消息里文件名 → 面板 → 编辑"同一条链）。
const enterEdit = (page, textPath, draft) => page.evaluate(({ textPath, draft }) => {
  const fp = window.__m.panel;
  fp.filePanelState.tabs = [{
    key: fp.filePanelTabKey(textPath),
    name: '生成结果.md',
    raw: textPath,
    info: {
      path: textPath, name: '生成结果.md', kind: 'text', savable: true, truncated: false,
      content: draft, size: draft.length, mtime: 1700000000000,
    },
    editing: false,
    draft: null,
  }];
  fp.filePanelState.activeKey = fp.filePanelState.tabs[0].key;
  fp.filePanelState.open = true;
  fp.renderFilePanel();
  fp.startFileEdit(fp.filePanelState.activeKey);
}, { textPath, draft });

async function preparePage(browser, { mobile }) {
  const context = await browser.newContext(mobile
    ? { viewport: { width: 360, height: 780 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true }
    : { viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', (e) => { if (!/^HTTP 404$/.test(e.message)) errors.push(`pageerror: ${e.message}`); });
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().includes('404') && !msg.text().includes('Failed to load resource')) {
      errors.push(`console.error: ${msg.text()}`);
    }
  });
  await page.addInitScript(() => { try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) {} });
  await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
  await page.waitForSelector('#messageInput', { timeout: 15000 });
  await page.waitForTimeout(300);
  await page.evaluate(async () => {
    window.__m = { panel: await import('./js/14-file-panel.js') };
    document.querySelectorAll('dialog[open]').forEach((d) => d.close());
  });
  return { context, page, errors };
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

    // ══ A. 手机形态 ══
    const mob = await preparePage(browser, { mobile: true });
    const m = mob.page;
    await enterEdit(m, TEXT_PATH, DRAFT);
    await m.waitForTimeout(150);

    // ── 前置：缺陷前提必须成立，否则下面每一条都是空断言 ──
    const pre = await m.evaluate(() => {
      const ta = document.querySelector('.file-edit-textarea');
      const area = document.querySelector('.file-edit-area');
      return {
        hasTextarea: Boolean(ta),
        insideEditArea: Boolean(area && ta && area.contains(ta)),
        value: ta?.value,
        editing: window.__m.panel.filePanelState.tabs[0].editing,
        panelVisible: document.querySelector('#filePanel')?.offsetWidth > 0,
      };
    });
    check('① 前置：编辑态真的进去了（textarea 在编辑区里、面板真的可见）',
      pre.hasTextarea && pre.insideEditArea && pre.editing && pre.panelVisible, JSON.stringify(pre));
    check('② 前置：草稿与内容一致（后续"选区还在"才有意义）', pre.value === DRAFT, JSON.stringify(pre.value));

    // ── A1. 重绘后编辑区存活 ──
    const after = await m.evaluate(({ draft }) => {
      const fp = window.__m.panel;
      const tab = fp.filePanelState.tabs[0];
      const ta = document.querySelector('.file-edit-textarea');
      ta.dataset.smokeTag = 'live';
      ta.setSelectionRange(2, 5);          // 模拟"刚全选/选了一段，正准备复制"
      // 不是"整块跳过重绘"：把工具栏要变的状态摆上，重绘后它必须真的更新。
      tab.reloading = true;
      fp.renderFilePanel();
      const now = document.querySelector('.file-edit-textarea');
      return {
        sameNode: now?.dataset.smokeTag === 'live',
        count: document.querySelectorAll('.file-edit-textarea').length,
        start: now?.selectionStart, end: now?.selectionEnd,
        draft: tab.draft, value: now?.value,
        toolbar: document.querySelector('.file-view-toolbar')?.textContent || '',
        draftExpected: draft,
      };
    }, { draft: DRAFT });
    check('③ 重绘后还是**同一个** textarea 节点（没有被 innerHTML 重建）',
      after.sameNode && after.count === 1, JSON.stringify(after));
    check('④ 选区原地保留（手机上正是"复制/粘贴菜单凭空消失"的那个位置）',
      after.start === 2 && after.end === 5, JSON.stringify({ start: after.start, end: after.end }));
    check('⑤ 草稿没丢（重建会把用户改到一半的内容换成磁盘版本）',
      after.draft === after.draftExpected && after.value === after.draftExpected, JSON.stringify(after.draft));
    check('⑥ 但不是"整块跳过重绘"：工具栏照样更新（刷新中… 出现了）',
      after.toolbar.includes('刷新中'), after.toolbar);

    // ── A2. 保存/取消在编辑区脚底、够大、在可视区内 ──
    const foot = await m.evaluate(() => {
      const f = document.querySelector('.file-edit-foot');
      const ta = document.querySelector('.file-edit-textarea');
      const save = document.querySelector('[data-file-save]');
      const fb = f.getBoundingClientRect();
      const tb = ta.getBoundingClientRect();
      return {
        inFoot: Boolean(save && f.contains(save)),
        saveCount: document.querySelectorAll('[data-file-save]').length,
        cancelCount: document.querySelectorAll('[data-file-edit-cancel]').length,
        toolbarButtons: document.querySelectorAll('.file-view-actions button').length,
        saveH: Math.round(save.getBoundingClientRect().height),
        saveW: Math.round(save.getBoundingClientRect().width),
        belowTextarea: fb.top >= tb.bottom - 1,
        visible: fb.bottom <= window.innerHeight + 1 && fb.top >= 0,
        hint: document.querySelector('.file-edit-hint')?.textContent || '',
      };
    });
    check('⑦ 「保存 / 取消」现在住在编辑区脚底（不在顶部工具栏）',
      foot.inFoot && foot.toolbarButtons === 0, JSON.stringify(foot));
    check('⑧ 只有一份按钮（挪位置而不是复制一份，否则两处会慢慢走偏）',
      foot.saveCount === 1 && foot.cancelCount === 1,
      JSON.stringify({ save: foot.saveCount, cancel: foot.cancelCount }));
    check('⑨ 操作条贴在 textarea 下方、且在可视区内（键盘弹起时随面板一起收缩）',
      foot.belowTextarea && foot.visible, JSON.stringify(foot));
    check('⑩ 手机按钮过 44px 最小点按尺寸',
      foot.saveH >= 44 && foot.saveW >= 44, JSON.stringify({ h: foot.saveH, w: foot.saveW }));
    check('⑪ 脚注文案是手机版：不提 Ctrl/⌘ 与 Esc（虚拟键盘上敲不出来）',
      !/Ctrl|⌘|Esc/.test(foot.hint) && foot.hint.includes('保存'), JSON.stringify(foot.hint));

    // ── A3. 手机编辑态也能直接点保存（端到端走到 saveFileTab，不靠快捷键）──
    await m.evaluate(() => {
      window.__saveHits = 0;
      const original = window.fetch;
      window.fetch = function patched(input, init) {
        if (String(input).includes('/file/save')) window.__saveHits += 1;
        return original.call(this, input, init);
      };
      document.querySelector('[data-file-save]').click();
    });
    await m.waitForTimeout(300);
    const saveHits = await m.evaluate(() => window.__saveHits);
    check('⑫ 点「保存」真的发起保存请求（手机没有 Ctrl+S，这条路是编辑态唯一出口）',
      saveHits === 1, `saveHits=${saveHits}`);
    check('⑬ 手机零 pageerror / 零 console.error（404 噪音除外）',
      mob.errors.length === 0, mob.errors.join(' | '));
    await mob.context.close();

    // ══ B. 桌面形态 ══
    const desk = await preparePage(browser, { mobile: false });
    const d = desk.page;
    await enterEdit(d, TEXT_PATH, DRAFT);
    await d.waitForTimeout(150);
    const deskState = await d.evaluate(() => {
      const fp = window.__m.panel;
      const tab = fp.filePanelState.tabs[0];
      const ta = document.querySelector('.file-edit-textarea');
      ta.dataset.smokeTag = 'live';
      ta.setSelectionRange(1, 3);
      fp.renderFilePanel();
      const now = document.querySelector('.file-edit-textarea');
      return {
        sameNode: now?.dataset.smokeTag === 'live',
        start: now?.selectionStart, end: now?.selectionEnd,
        hint: document.querySelector('.file-edit-hint')?.textContent || '',
        inFoot: document.querySelector('.file-edit-foot')?.contains(document.querySelector('[data-file-save]')),
        tabOpen: tab.editing,
      };
    });
    check('⑭ 桌面同样不重建编辑区（选区原地保留）',
      deskState.sameNode && deskState.start === 1 && deskState.end === 3, JSON.stringify(deskState));
    check('⑮ 桌面脚注保留快捷键说明（桌面真有 Ctrl/⌘ 与 Esc）',
      /Ctrl|⌘/.test(deskState.hint) && deskState.hint.includes('Esc'), JSON.stringify(deskState.hint));
    check('⑯ 桌面保存按钮也在脚底（两个平台同一份 DOM，只有文案与尺寸按平台切）',
      deskState.inFoot === true, JSON.stringify(deskState));
    check('⑰ 桌面零 pageerror / 零 console.error（404 噪音除外）',
      desk.errors.length === 0, desk.errors.join(' | '));
    await desk.context.close();

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
