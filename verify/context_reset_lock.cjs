// 「重置上下文后前端锁不解除」的浏览器侧检查（由 verify/context_reset_lock.py 自编排：
// 它先起隔离真实源码实例（独立 data_dir / config / 端口），再把地址经 NAIBA_SMOKE_BASE 传进来）。
//
// 两部分：
//   A. 真数据（隔离库播种满量 usage）—— 真库 → 真接口 → 真渲染，覆盖触发口 ②（手动「新会话」）
//      与 ①的终态形态（模型重置留下的分割线 + 种子消息），并留证三张图。
//   B. 模块级注入 —— 单消息入口（done 事件同带满量 usage 与 session_start）、运行中保护、
//      遗留 role=session 标记行；这些形态靠真数据不好造，直接调真实模块函数考。
//
// 环境变量：NAIBA_SMOKE_BASE（必填）、NAIBA_CONTEXT_RESET_LIMIT（上下文上限，默认 100000）
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || '';
const LIMIT = Number(process.env.NAIBA_CONTEXT_RESET_LIMIT || '100000');
const ROOT = path.resolve(__dirname, '..');

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

// 输入框/圆环/分割线的共同快照：锁的三个可观察面（禁用态、占位符、发送键）+ 圆环数值。
async function snapshot(page) {
  return page.evaluate(() => {
    const input = document.querySelector('#messageInput');
    const send = document.querySelector('#sendButton');
    const ring = document.querySelector('#contextUsageRing');
    const label = document.querySelector('#contextPercentLabel');
    return {
      disabled: Boolean(input && input.disabled),
      placeholder: (input && input.placeholder) || '',
      value: (input && input.value) || '',
      sendDisabled: Boolean(send && send.disabled),
      sendTitle: (send && send.title) || '',
      ring: ((ring && ring.style.getPropertyValue('--context-percent')) || '').trim(),
      label: ((label && label.textContent) || '').trim(),
      dividers: document.querySelectorAll('#messages .session-divider').length,
      seedButtons: document.querySelectorAll('#messages [data-fill-reset-seed]').length,
    };
  });
}

(async () => {
  if (!BASE) {
    console.log('FAIL  缺少 NAIBA_SMOKE_BASE（请通过 verify/context_reset_lock.py 运行）');
    process.exit(1);
  }
  let code = 1;
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const pageErrors = [];
    const badResponses = [];
    page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
    page.on('console', (msg) => {
      // 「Failed to load resource」不带 URL，无法与已知噪音区分；接口层的 4xx/5xx 由
      // badResponses 逐条带 URL 断言，所以这里只收真正的 console.error 文案。
      if (msg.type() === 'error' && !msg.text().includes('Failed to load resource')) {
        pageErrors.push(`console.error: ${msg.text()}`);
      }
    });
    // 已知取样噪音：隔离实例的供应商是 `https://example.invalid/v1`（_serve_tmp.py 的占位），
    // 模型目录必然连不通 → 后端回 400。其余任何 4xx/5xx 照旧算失败。
    const KNOWN_NOISE = /\/api\/providers\/models$/;
    page.on('response', (res) => {
      if (res.status() >= 400 && !KNOWN_NOISE.test(res.url())) badResponses.push(`${res.status()} ${res.url()}`);
    });
    // 「新会话」「撤销分割线」都走 window.confirm。
    page.on('dialog', (dialog) => dialog.accept());

    await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 30000 });
    // 关掉「会话同步」轮询（10s 一跳会按服务端真实状态重渲染）：B 部分往 state 里注入的
    // 夹具必须活到断言那一刻，否则又是 §九.90 那个错法（拍到的不是被断言的状态）。
    await page.evaluate(async () => {
      const core = await import('./js/01-core.js');
      core.state.syncPolling = false;
      if (core.state.syncTimer) { window.clearTimeout(core.state.syncTimer); core.state.syncTimer = null; }
    });

    const openConversation = async (marker, expectText) => {
      await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${marker}") .conversation-open`);
      await page.waitForSelector(`#messages .message-row:has-text("${expectText}")`, { timeout: 20000 });
      await page.waitForTimeout(800);
    };

    // ─────────────────── A. 真数据 ───────────────────
    // ① 满量会话：打开即锁
    await openConversation('已经聊满了', '满量回复');
    let seen = await snapshot(page);
    check('A① 满量会话打开即锁（输入框禁用 + 占位符 + 圆环 100%）',
      seen.disabled && seen.placeholder.includes('上下文已满')
      && seen.ring === '100.0' && seen.label === '上下文 100.0%'
      && seen.dividers === 0 && seen.sendTitle.includes('上下文已满'),
      JSON.stringify(seen));
    await page.screenshot({ path: path.join(ROOT, 'verify', 'context_reset_lock_1_locked.png') });

    // ② 点真实「新会话」按钮 → 圆环归零、输入框解锁（终端用户被卡住的那一步）
    await page.click('#messages .message-row[data-message-id] [data-session-start-after]');
    await page.waitForTimeout(1200);
    seen = await snapshot(page);
    check('A② 点「新会话」后圆环归零、输入框解锁',
      !seen.disabled && seen.placeholder === '输入消息' && seen.ring === '0'
      && seen.label === '上下文 --' && seen.dividers === 1,
      JSON.stringify(seen));
    // 解锁不是"换个锁法"：真的能打字、发送键真的亮起来
    await page.fill('#messageInput', '分割线之后继续');
    await page.waitForTimeout(150);
    seen = await snapshot(page);
    check('A② 解锁后输入框可用（打字 → 发送键可用）',
      !seen.disabled && !seen.sendDisabled, JSON.stringify({ disabled: seen.disabled, send: seen.sendDisabled }));
    await page.screenshot({ path: path.join(ROOT, 'verify', 'context_reset_lock_2_unlocked.png') });

    // ③ 撤销分割线 → 满量重新生效、按需回锁（同一扫描天然覆盖，无需特殊分支）
    await page.fill('#messageInput', '');
    await page.click('#messages [data-cancel-session-start]');
    await page.waitForTimeout(1200);
    seen = await snapshot(page);
    check('A③ 撤销分割线后满量重新生效、按需回锁',
      seen.disabled && seen.ring === '100.0' && seen.dividers === 0 && seen.placeholder.includes('上下文已满'),
      JSON.stringify(seen));

    // ④ 模型主动重置（source=tool）留下的分割线：打开即解锁 + 种子进**可用**输入框
    await openConversation('请写交接文档', '已交接');
    seen = await snapshot(page);
    check('A④ 模型重置的会话打开即解锁（分割线在末尾 → 有效上下文≈0）',
      !seen.disabled && seen.ring === '0' && seen.dividers === 1 && seen.seedButtons === 1,
      JSON.stringify(seen));
    await page.click('#messages [data-fill-reset-seed]');
    await page.waitForTimeout(400);
    seen = await snapshot(page);
    check('A④ 种子消息填进可用输入框（用户卡死的正是这一步）',
      !seen.disabled && !seen.sendDisabled
      && seen.value.includes('交接-冒烟.md') && seen.value.includes('job_1'),
      JSON.stringify({ disabled: seen.disabled, send: seen.sendDisabled, value: seen.value.slice(0, 100) }));
    await page.locator('.composer-wrap').screenshot({
      path: path.join(ROOT, 'verify', 'context_reset_lock_3_seed_ready.png'),
    });

    // ⑤ 分割线之下还有更新（更小）的用量 → 圆环按线以下算
    await openConversation('线下用量', '第二答');
    seen = await snapshot(page);
    check('A⑤ 分割线之下有更小用量时按线以下的用量算（10,000/100,000 = 10%）',
      !seen.disabled && seen.ring === '10.0' && seen.label === '上下文 10.0%',
      JSON.stringify(seen));

    // ─────────────────── B. 模块级注入 ───────────────────
    const b = await page.evaluate(async (limit) => {
      const core = await import('./js/01-core.js');
      const media = await import('./js/03-media.js');
      core.state.conversationId = 'ctx-fixture';
      core.state.messagesConversationId = 'ctx-fixture';
      core.state.chatBusy = false;
      core.state.editingMessageId = '';
      core.state.bootstrap = core.state.bootstrap || {};
      core.state.bootstrap.model_profiles = [{ model_key: 'fixture', context_window: limit }];
      const input = document.querySelector('#messageInput');
      const send = document.querySelector('#sendButton');
      const ring = document.querySelector('#contextUsageRing');
      const label = document.querySelector('#contextPercentLabel');
      const snap = () => ({
        atCeiling: Boolean(core.state.contextAtCeiling),
        disabled: Boolean(input.disabled),
        placeholder: input.placeholder,
        sendDisabled: Boolean(send.disabled),
        ring: (ring.style.getPropertyValue('--context-percent') || '').trim(),
        label: (label.textContent || '').trim(),
      });
      const usage = (context) => ({
        input_tokens: context - 500,
        output_tokens: 500,
        total_tokens: context,
        context_tokens: context,
        context_limit: limit,
        context_limit_source: 'local_config',
        model_key: 'fixture',
        requests: 1,
      });
      input.value = '';
      const out = {};
      // B1 单消息入口（done/error/取消 三个终态都走它）：满量、无分割线 → 锁
      media.updateContextUsage(null, { id: 'b1', role: 'assistant', metadata: { usage: usage(limit) } });
      out.terminalLocked = snap();
      // B1' 同一条消息，但带 session_start（reset_context 成功时 done 事件的真实形态：
      //     满量 usage 与 session_start 同时挂在一条消息上）→ 必须解锁
      media.updateContextUsage(null, {
        id: 'b1',
        role: 'assistant',
        metadata: { usage: usage(limit), session_start: { at: Date.now(), source: 'tool' } },
      });
      out.terminalReset = snap();
      // B2 运行中保护：实时值不得被「无 usage 的历史」擦回「暂无数据」（原有回归）
      media.updateContextUsage(null, { id: 'b2', role: 'assistant', metadata: { usage: usage(limit) } });
      out.beforeBusy = snap();
      core.state.chatBusy = true;
      media.updateContextUsage([]);
      out.busyNoData = snap();
      // B2' 但「上下文已重置」必须越过运行中保护（收尾瞬间 chatBusy 尚未落下）
      media.updateContextUsage([
        { id: 'u1', role: 'user' },
        { id: 'a1', role: 'assistant', metadata: { usage: usage(limit), session_start: { at: Date.now(), source: 'tool' } } },
      ]);
      out.busyReset = snap();
      core.state.chatBusy = false;
      // B3 遗留形态：早期版本用独立的 role=session 标记行 —— 同样是分割线
      media.updateContextUsage([
        { id: 'u2', role: 'user' },
        { id: 'a2', role: 'assistant', metadata: { usage: usage(limit) } },
        { id: 'legacy', role: 'session' },
      ]);
      out.legacyReset = snap();
      return out;
    }, LIMIT);

    const locked = { atCeiling: true, disabled: true, ring: '100.0', placeholder: '上下文已满，请新建对话后继续' };
    const unlocked = { atCeiling: false, disabled: false, ring: '0', placeholder: '输入消息' };
    const matches = (snap, want) => Object.keys(want).every((key) => snap[key] === want[key]);

    check('B1 单消息入口 · 满量无分割线 → 仍锁', matches(b.terminalLocked, locked), JSON.stringify(b.terminalLocked));
    check('B1 单消息入口 · 满量 + session_start（模型重置的 done 形态）→ 解锁',
      matches(b.terminalReset, unlocked), JSON.stringify(b.terminalReset));
    check('B2 运行中 · 无 usage 的历史重渲染不得擦掉实时值',
      matches(b.busyNoData, locked), JSON.stringify(b.busyNoData));
    check('B2 运行中 · 但上下文已重置必须越权解锁',
      matches(b.busyReset, unlocked), JSON.stringify(b.busyReset));
    check('B3 遗留 role=session 标记行同样是分割线',
      matches(b.legacyReset, unlocked), JSON.stringify(b.legacyReset));

    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
    check('零失败接口请求', badResponses.length === 0, JSON.stringify(badResponses.slice(0, 3)));

    code = failures.length ? 1 : 0;
  } catch (error) {
    check('冒烟脚本自身异常', false, String((error && error.message) || error));
  } finally {
    await browser.close();
  }
  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(code);
})();
