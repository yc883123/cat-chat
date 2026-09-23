// 手机回车语义守门（静态 public/ 服务，无需后端）——§九.135 第 1 项的回归防线。
//
// 缺陷形态：`#messageInput` 的 keydown 里 `Enter && !shiftKey && !isComposing` 一律发送
// （运行中则入队），**没有平台分支**。桌面合理；手机上虚拟键盘的回车就成了「发送」，
// 而虚拟键盘**没有 Shift 键** ⇒ 用户永远敲不出换行。第二处同类在插话队列的「行内编辑」框
// （`18-interjections.js`），只改一处会让同一个手机上两个输入框行为正好相反。
//
// 修法：`01-core.isCoarsePointer()`（判据只用 `(pointer: coarse)` 媒体查询，不看宽度——
// 把窗口拖窄的桌面浏览器仍有物理 Shift 键，行为不该跟着窗口宽度变）：
//   · 粗指针 + 裸回车 = 换行（不 preventDefault，直接放行）；
//   · 粗指针 + Ctrl/Cmd+Enter 仍发送/入队（照顾外接键盘）；
//   · 细指针（桌面）行为零变化。
//
// 为什么静态服务就够：本条考的是**按键分支决策**与「有没有把请求发出去」，与后端数据无关。
// 真后端下的插话整链路（真入队 / 引导 / 冻结）由 `verify/interjection_smoke.py` 覆盖。
//
// 用法：$env:NODE_PATH="<node_modules>"; node verify\composer_enter_mobile_smoke.cjs
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = Number(process.env.NAIBA_ENTER_SMOKE_PORT || 8817);
const BASE = `http://127.0.0.1:${PORT}`;
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const SETTLE_MS = 300;      // 版式过渡（transition: var(--t-fast) = 120ms all）的稳定等待
const DRAFT = '第一行草稿';

const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

// 页面内助手：拿真模块。`isCoarsePointer` 必须调**真函数**——直接断言 matchMedia 字符串
// 等于在断言「浏览器实现」，而不是断言产品判据。
const PROBE = () => {
  const t = document.querySelector('#messageInput');
  window.__probe = {
    coarse: null,
    setState({ busy = false, runId = '' } = {}) {
      const st = window.__m.core.state;
      st.chatBusy = busy;
      st.chatRunId = runId;
      // 静态服务没有 /api/bootstrap ⇒ 会话 id 是空的，而入队链路第一关就要求它非空
      // （`sendRunInterjection` 无会话直接 return null）。凡是要走真实入队/发送的用例都得先立这个前提，
      // 否则「没有请求」会被误读成「回车判定坏了」——这正是本冒烟第一版踩的坑。
      st.conversationId = st.conversationId || 'enter-smoke-conv';
      window.__m.media.updateContextComposerLock(busy);
    },
    // 真正走发送链路的前置条件：`sendChatMessage` 会在「没选供应商 / 模型未验证」时直接
    // toast + return（**不发请求**）。静态服务下这两项天然是空的 ⇒ 不立这个前提，
    // 「桌面回车没发送」会被误读成回车判定坏了（本冒烟第一版就栽在这里）。
    primeSend() {
      const st = window.__m.core.state;
      st.bootstrap = st.bootstrap || {};
      st.bootstrap.model_profiles = [{ model_key: 'enter-smoke-provider', name: '冒烟供应商' }];
      st.providerModelCatalogs = st.providerModelCatalogs || {};
      st.providerModelCatalogs['enter-smoke-provider'] = [{ id: 'enter-smoke-model' }];
      const sel = document.querySelector('#modelSelect');
      if (sel) { sel.innerHTML = '<option value="enter-smoke-provider">冒烟</option>'; sel.value = 'enter-smoke-provider'; }
      const csel = document.querySelector('#composerModelSelect');
      if (csel) { csel.innerHTML = '<option value="enter-smoke-model">冒烟模型</option>'; csel.value = 'enter-smoke-model'; }
    },
    canSend() { return Boolean(window.__m.models.selectedProvider() && window.__m.models.composerModelIsValidated()); },
    placeholder(sel = '#messageInput') { return document.querySelector(sel)?.placeholder || ''; },
    value() { return t.value; },
    setValue(v) { t.value = v; window.__m.core.notifyComposerChanged(t); },
    rows() { return document.querySelectorAll('#runGuidanceList .run-guidance-card').length; },
    closeDialogs() {
      // 静态 public/ 下 /api/bootstrap 必 404 ⇒ `05-bootstrap.js` 会弹 #authDialog **模态**，
      // 真实点击会被它拦住、Playwright 一路重试到 30s 超时（§九.134 的探针环境坑）。
      document.querySelectorAll('dialog[open]').forEach((d) => d.close());
    },
  };
};

async function openPage(browser, contextOptions) {
  const ctx = await browser.newContext(contextOptions);
  const page = await ctx.newPage();
  const errors = [];
  // 静态 public/ 服务下 `/api/bootstrap` 必然 404，而它是以**未捕获异常**形式冒出来的
  // （`pageerror: HTTP 404`），不是 console 噪音 ⇒ 只放行这一种「整条消息就是 HTTP 404」的形态，
  // 其余 pageerror 一律算失败（别用 startsWith('HTTP') 之类的宽口径，那会把真故障一起吞掉）。
  page.on('pageerror', (e) => { if (!/^HTTP 404$/.test(e.message)) errors.push(`pageerror: ${e.message}`); });
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().includes('404') && !msg.text().includes('Failed to load resource')) {
      errors.push(`console.error: ${msg.text()}`);
    }
  });
  await page.addInitScript(() => { try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (_) {} });
  await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
  await page.waitForSelector('#messageInput', { timeout: 15000 });
  await page.waitForTimeout(400);
  await page.evaluate(async () => {
    // eslint-disable-next-line no-undef
    window.__m = {
      core: await import('./js/01-core.js'),
      media: await import('./js/03-media.js'),
      interjections: await import('./js/18-interjections.js'),
      models: await import('./js/07-models-agents.js'),
    };
  });
  await page.evaluate(PROBE);
  // 静态 public/ 下 /api/bootstrap 必 404 ⇒ 首启/鉴权弹层会以 `dialog[open]:modal` 出现，
  // 真实点击会被它整层拦住（Playwright 一路重试到 30s 超时，现象是"卡住"而不是报错）。
  await page.evaluate(() => window.__probe.closeDialogs());
  return { ctx, page, errors };
}

// 三个计数桩：/api/chat（发送）、/api/chat/interject（入队）、/api/chat/interject/edit（改插话）
async function stubApis(page) {
  const calls = { chat: [], interject: [], interjectEdit: [] };
  await page.route('**/api/chat', async (route) => {
    let body = {};
    try { body = route.request().postDataJSON() || {}; } catch (_) { body = {}; }
    calls.chat.push(body);
    const runId = `enter-smoke-run-${calls.chat.length}`;
    const stream = [
      { type: 'run_started', run_id: runId, sequence: 1 },
      { type: 'delta', run_id: runId, content: '冒烟答复', sequence: 2 },
      {
        type: 'done',
        run_id: runId,
        sequence: 3,
        message: { id: `enter-smoke-assistant-${calls.chat.length}`, role: 'assistant', content: '冒烟答复', created_at: Date.now(), metadata: {} },
      },
    ].map((e) => JSON.stringify(e)).join('\n') + '\n';
    await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: stream });
  });
  await page.route('**/api/chat/interject/edit', async (route) => {
    calls.interjectEdit.push(route.request().postDataJSON() || {});
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
  });
  await page.route('**/api/chat/interject', async (route) => {
    const body = route.request().postDataJSON() || {};
    calls.interject.push(body);
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        message: {
          id: `enter-smoke-ij-${calls.interject.length}`,
          role: 'user',
          content: String(body.message || ''),
          created_at: Date.now(),
          metadata: { interjection: true, interjection_guided: false, attachments: [] },
        },
      }),
    });
  });
  return calls;
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

    // ═══════════ A. 手机形态 360×780 · DPR3 · touch ═══════════
    const { ctx: mctx, page: mpage, errors: merrors } = await openPage(browser, {
      viewport: { width: 360, height: 780 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true,
    });
    const calls = await stubApis(mpage);
    const setState = (opts) => mpage.evaluate((o) => window.__probe.setState(o), opts);
    const value = () => mpage.evaluate(() => window.__probe.value());
    const setValue = (v) => mpage.evaluate((x) => window.__probe.setValue(x), v);

    // ── 前提：粗指针判据真的为真（伪造不出来的一条）
    const coarse = await mpage.evaluate(() => window.__m.core.isCoarsePointer());
    check('① 前提成立：真模块 isCoarsePointer() 在手机形态下为 true', coarse === true, String(coarse));

    // ── 手机：裸回车 = 换行，且**一个请求都不许发出去**
    await setState({ busy: false });
    await setValue('');
    await mpage.evaluate(() => window.__probe.closeDialogs());
    await mpage.click('#messageInput');
    await mpage.keyboard.type(DRAFT);
    await mpage.keyboard.press('Enter');
    await mpage.keyboard.type('第二行');
    const v = await value();
    check('② 手机裸回车插入的是换行（文本变成两行、光标留在原地）',
      v === `${DRAFT}\n第二行`, JSON.stringify(v));
    check('③ 手机裸回车没有走发送链路（/api/chat 请求数为 0）',
      calls.chat.length === 0, `chat=${calls.chat.length}`);

    // ── 手机：运行中裸回车同样只换行（不许悄悄入队）
    await setState({ busy: true, runId: 'enter-smoke-run' });
    await sleep(SETTLE_MS);
    await setValue('');
    await mpage.evaluate(() => window.__probe.closeDialogs());
    await mpage.click('#messageInput');
    await mpage.keyboard.type(DRAFT);
    await mpage.keyboard.press('Enter');
    await mpage.keyboard.type('续');
    check('④ 手机运行中裸回车 = 换行，不触发入队（/api/chat/interject 请求数为 0）',
      (await value()) === `${DRAFT}\n续` && calls.interject.length === 0,
      JSON.stringify({ value: await value(), interject: calls.interject.length }));

    // ── 连带文案：手机运行中的 placeholder 不许再承诺「Enter 加入队列」（第 1 项的直接后果）
    const busyPh = await mpage.evaluate(() => window.__probe.placeholder());
    check('④-1 手机运行中占位符不再承诺 Enter（改说「点插话键」），也不含 Shift/Esc',
      !busyPh.includes('Enter') && busyPh.includes('插话键') && !/Shift|Esc/.test(busyPh),
      JSON.stringify(busyPh));

    // ── 硬验收 2：插话键 ≥44px（它现在是运行中唯一的插话入口）
    const ijBox = await mpage.evaluate(() => {
      const r = document.querySelector('#interjectButton').getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height) };
    });
    check('⑤ 插话键 ≥44×44（运行中唯一的插话入口，必须过手机最小点按尺寸）',
      ijBox.w >= 44 && ijBox.h >= 44, JSON.stringify(ijBox));

    // ── 硬验收 1：**点**插话键真入队（不是只断言按钮显隐）
    await mpage.evaluate(() => window.__probe.closeDialogs());
    await mpage.click('#interjectButton');
    await mpage.waitForTimeout(300);
    const posted = calls.interject[0];
    check('⑥ 手机点插话键真的发出了入队请求，且请求体里就是草稿原文',
      calls.interject.length === 1 && posted && posted.message === `${DRAFT}\n续`
      && posted.conversation_id === (await mpage.evaluate(() => window.__m.core.state.conversationId)),
      JSON.stringify({ count: calls.interject.length, body: posted }));
    check('⑦ 入队后队列面板里真的出现了这一条（不是只断言按钮状态）',
      (await mpage.evaluate(() => window.__probe.rows())) === 1
      && (await mpage.evaluate(() => document.querySelector('#runGuidanceList').hidden)) === false,
      JSON.stringify({ rows: await mpage.evaluate(() => window.__probe.rows()) }));

    // ── 第二处同类：插话队列「行内编辑」框必须与输入框同判定（共用一个 isCoarsePointer）
    await mpage.click('[data-interjection-edit]');
    await mpage.waitForTimeout(200);
    const editSel = '#runGuidanceList .run-guidance-input';
    check('⑧ 点铅笔进入行内编辑（编辑框与「保存 / 取消」都在）',
      (await mpage.locator(editSel).count()) === 1
      && (await mpage.locator('[data-interjection-cancel]').count()) === 1);
    const editPh = await mpage.evaluate((sel) => window.__probe.placeholder(sel), editSel);
    check('⑧-1 插话编辑框的手机占位符只说「保存 / 取消」，不承诺 Enter 保存与 Esc 取消',
      !/Shift|Esc/.test(editPh) && editPh.includes('保存') && editPh.includes('取消'),
      JSON.stringify(editPh));
    await mpage.click(editSel);
    await mpage.keyboard.press('End');
    await mpage.keyboard.press('Enter');
    await mpage.keyboard.type('追加');
    const editValue = await mpage.inputValue(editSel);
    check('⑨ 插话编辑框：手机裸回车 = 换行，不提交（edit 请求数为 0）',
      editValue.includes('\n') && editValue.endsWith('追加') && calls.interjectEdit.length === 0,
      JSON.stringify({ editValue, edits: calls.interjectEdit.length }));
    await mpage.click('[data-interjection-save]');
    await mpage.waitForTimeout(300);
    check('⑩ 编辑框点「保存」才是真的提交（edit 请求数 = 1，且正文含刚敲的换行）',
      calls.interjectEdit.length === 1
      && String(calls.interjectEdit[0].message || '').includes('追加'),
      JSON.stringify(calls.interjectEdit[0]));

    // ── 编辑消息态：手机文案同样不许提 Shift / Esc（同一处契约的收尾）
    const editMsgPh = await mpage.evaluate(async () => {
      const st = window.__m.core.state;
      st.editingMessageId = 'enter-smoke-msg';
      window.__m.media.updateContextComposerLock(false);
      const ph = document.querySelector('#messageInput').placeholder;
      st.editingMessageId = '';
      window.__m.media.updateContextComposerLock(false);
      return ph;
    });
    check('⑩-1 手机「编辑消息」占位符不承诺 Shift+Enter / Esc（手机没有这两个键）',
      !/Shift|Esc/.test(editMsgPh) && editMsgPh.includes('重新发送'),
      JSON.stringify(editMsgPh));

    // ── 手机：Ctrl/Cmd+Enter 仍能发送（外接键盘的路不能一起堵死）
    await mpage.evaluate(() => { window.__probe.primeSend(); window.__probe.closeDialogs(); });
    await setState({ busy: false });
    await sleep(SETTLE_MS);
    await setValue('');
    check('⑪-0 前提成立：发送前置条件齐备（选中供应商 + 模型已验证）',
      (await mpage.evaluate(() => window.__probe.canSend())) === true);
    await mpage.evaluate(() => window.__probe.closeDialogs());
    await mpage.click('#messageInput');
    await mpage.keyboard.type('外接键盘草稿');
    await mpage.keyboard.press('Control+Enter');
    await mpage.waitForTimeout(400);
    check('⑪ 手机 + Ctrl+Enter 仍然发送（外接键盘不吃亏）',
      calls.chat.length === 1 && String(calls.chat[0].message || '').includes('外接键盘草稿'),
      JSON.stringify({ count: calls.chat.length, body: calls.chat[0] }));

    check('⑫ 手机阶段零 pageerror / 零 console.error（404 噪音除外）',
      merrors.length === 0, merrors.join(' | '));
    await mctx.close();

    // ═══════════ B. 桌面形态 1440（行为零变化） ═══════════
    const { ctx: dctx, page: dpage, errors: derrors } = await openPage(browser, { viewport: { width: 1440, height: 900 } });
    const dcalls = await stubApis(dpage);
    const dCoarse = await dpage.evaluate(() => window.__m.core.isCoarsePointer());
    check('⑬ 前提成立：桌面形态下 isCoarsePointer() 为 false', dCoarse === false, String(dCoarse));
    await dpage.evaluate(() => { window.__probe.primeSend(); window.__probe.setState({ busy: false }); });
    const dBusyPh = await dpage.evaluate(async () => {
      window.__probe.setState({ busy: true, runId: 'enter-smoke-run' });
      const ph = document.querySelector('#messageInput').placeholder;
      window.__probe.setState({ busy: false });
      return ph;
    });
    check('⑭-0 桌面运行中占位符仍然是「输入后 Enter 加入插话队列」（桌面行为零变化）',
      dBusyPh.includes('Enter') && dBusyPh.includes('插话队列'), JSON.stringify(dBusyPh));
    check('⑬-0 前提成立：桌面发送前置条件齐备',
      (await dpage.evaluate(() => window.__probe.canSend())) === true);
    await dpage.evaluate(() => window.__probe.closeDialogs());
    await dpage.click('#messageInput');
    await dpage.keyboard.type('桌面回车应发送');
    await dpage.keyboard.press('Enter');
    await dpage.waitForTimeout(400);
    check('⑭ 桌面裸回车仍然是发送（行为零变化）',
      dcalls.chat.length === 1 && String(dcalls.chat[0].message || '').includes('桌面回车应发送'),
      JSON.stringify({ count: dcalls.chat.length, body: dcalls.chat[0] }));
    check('⑮ 桌面发送后输入框被清空（真走了发送链路，不是只发了请求）',
      (await dpage.evaluate(() => window.__probe.value())) === '',
      JSON.stringify(await dpage.evaluate(() => window.__probe.value())));
    check('⑯ 桌面阶段零 pageerror / 零 console.error（404 噪音除外）',
      derrors.length === 0, derrors.join(' | '));
    await dctx.close();

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
