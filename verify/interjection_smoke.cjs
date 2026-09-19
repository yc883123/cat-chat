// 插话队列 + 流式状态「重连中…」滞留的冒烟（由 verify/interjection_smoke.py 编排，真后端）。
//
// 覆盖两个计划（2026-09-19）：
//   · 插话：运行中入队（真 POST 落库）/ 编辑 / 引导 / 删除 / 冻结后取回；与选择面板互不锁死。
//   · 重连状态：重连恢复后 #runtimeStatus 不滞留「重连中…」，且不得越权清掉「正在思考 · 已等待 X 秒」。
//
// 判据一律写到**用户可见的东西**（状态栏文本、面板行、按钮是否可点、库里是否真落了行），
// 不断言内部变量——「按钮存在但用户用不了」是这套代码最常见的回归形态（维护说明 §九）。
//
// 事件流是页面内合成 SSE：真跑一轮要模型，而这里要验的是**前端对事件的反应**，
// 与模型无关。fetch 拦截在 addInitScript 里装好，所以 resumeConversationRun 走的是真路径。
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8796';
const ACTIVE_TITLE = process.env.NAIBA_SMOKE_ACTIVE_TITLE || '插话冒烟';
const CHOICE_TITLE = process.env.NAIBA_SMOKE_CHOICE_TITLE || '插话选择共存冒烟';
const ACTIVE_ID = process.env.NAIBA_SMOKE_ACTIVE_ID || '';
const RUN_ID = process.env.NAIBA_SMOKE_RUN_ID || '';
const ROOT = path.resolve(__dirname, '..');
const failures = [];

function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(pathname, options = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(`${BASE}${pathname}`, init);
  return response.json().catch(() => ({}));
}

async function messagesOf(conversationId) {
  const data = await apiJson(`/api/conversations/${encodeURIComponent(conversationId)}`);
  return data.messages || [];
}

/** 队列面板快照：整块的状态 + 每行「谁、什么状态、有哪些动作」。 */
async function panelSnapshot(page) {
  return page.evaluate(() => {
    const host = document.querySelector('#runGuidanceList');
    const rows = [...(host?.querySelectorAll('.run-guidance-card') || [])];
    return {
      hidden: host ? host.hidden : true,
      head: host?.querySelector('.run-guidance-head')?.textContent || '',
      rows: rows.map((row) => ({
        id: row.dataset.messageId || '',
        runId: row.dataset.runId || '',
        state: row.dataset.interjectionState || '',
        badge: row.querySelector('.run-guidance-badge')?.textContent || '',
        text: row.querySelector('.run-guidance-preview')?.textContent || '',
        actions: [...row.querySelectorAll('.run-guidance-actions button')]
          .map((button) => Object.keys(button.dataset)[0] || '')
          .filter(Boolean)
          .map((key) => key.replace('interjection', '').toLowerCase()),
        editing: Boolean(row.querySelector('.run-guidance-input')),
      })),
    };
  });
}

/** 输入区 / 状态栏 / 插话按钮的可用性快照（「能不能用」而不是「在不在」）。 */
async function availabilitySnapshot(page) {
  return page.evaluate(() => {
    const interject = document.querySelector('#interjectButton');
    const send = document.querySelector('#sendButton');
    const input = document.querySelector('#messageInput');
    return {
      status: document.querySelector('#runtimeStatus')?.textContent || '',
      interjectHidden: Boolean(interject?.hidden),
      interjectDisabled: Boolean(interject?.disabled),
      interjectTitle: interject?.title || '',
      sendTitle: send?.title || '',
      inputDisabled: Boolean(input?.disabled),
      placeholder: input?.placeholder || '',
      choiceButtons: document.querySelectorAll('#choiceButtons button[data-choice-value]').length,
      choiceDisabled: [...document.querySelectorAll('#choiceButtons button[data-choice-value]')]
        .filter((button) => button.disabled).length,
    };
  });
}

function rowByText(snapshot, needle) {
  return snapshot.rows.find((row) => row.text.includes(needle));
}

/** 失败诊断用：状态栏时间线 + 流句柄 + 运行态内部量（只做诊断，不做断言）。 */
async function diag(page) {
  return page.evaluate(async () => {
    const core = await import('./js/01-core.js');
    const now = Date.now();
    return {
      streams: window.__streams.map((handle) => handle.closed),
      statusLog: window.__statusLog.map(([at, text]) => `${now - at}ms前 ${text}`),
      chatRunId: core.state.chatRunId,
      chatBusy: core.state.chatBusy,
      elapsedKind: core.state.elapsedReconnectShown,
      elapsedBase: core.state.elapsedBase,
      connectionState: core.state.connectionState,
      runAttempt: core.state.runAttempt,
    };
  });
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    // 隔离实例的供应商是 example.invalid 占位：模型目录必然连不通（http 400），
    // 那是环境噪音而非缺陷，其余 console.error 照旧算失败。
    if (msg.type() === 'error' && !msg.text().includes('Failed to load resource')) {
      pageErrors.push(`console.error: ${msg.text()}`);
    }
  });
  page.on('dialog', (dialog) => dialog.accept());

  // 合成事件流：把 /api/runs/*/events 换成本地可控的 ReadableStream。
  // 这样 resumeRun / consumeRunStream / handleChatEvent 全是**真代码**，只有字节来源是假的。
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window);
    const encoder = new TextEncoder();
    window.__streams = [];
    // 事件流「建流耗时」旋钮：默认 0（立刻建好）。验重连横幅时调大一点，才能稳定观测到
    // 「重连中…」那段窗口——真机上后端毫秒级握手，横幅几乎一闪而过。
    window.__eventsOpenDelay = 0;
    // 状态栏文本时间线：诊断「重连中…」到底是被清了、还是又被谁重新写上了。
    window.__statusLog = [];
    document.addEventListener('DOMContentLoaded', () => {
      const el = document.querySelector('#runtimeStatus');
      if (!el) return;
      new MutationObserver(() => {
        const last = window.__statusLog[window.__statusLog.length - 1];
        if (!last || last[1] !== el.textContent) {
          window.__statusLog.push([Date.now(), el.textContent]);
        }
      }).observe(el, { childList: true, characterData: true, subtree: true });
    });
    window.fetch = (input, init = {}) => {
      const url = typeof input === 'string' ? input : String((input && input.url) || '');
      if (/\/api\/runs\/[^/]+\/events/.test(url)) {
        const handle = { closed: false };
        const stream = new ReadableStream({
          start(controller) {
            handle.push = (event) => {
              if (handle.closed) return;
              controller.enqueue(encoder.encode(`${JSON.stringify(event)}\n`));
            };
            handle.fail = () => {
              handle.closed = true;
              controller.error(new Error('synthetic net down'));
            };
            handle.close = () => {
              handle.closed = true;
              try { controller.close(); } catch (_) { /* 已关闭 */ }
            };
          },
        });
        window.__streams.push(handle);
        const response = () => new Response(stream, {
          status: 200,
          headers: { 'Content-Type': 'application/x-ndjson' },
        });
        const delay = Number(window.__eventsOpenDelay || 0);
        if (!delay) return Promise.resolve(response());
        return new Promise((resolve) => window.setTimeout(() => resolve(response()), delay));
      }
      return originalFetch(input, init);
    };
  });

  const settlePollingOff = () => page.evaluate(async () => {
    const core = await import('./js/01-core.js');
    core.state.syncPolling = false;
    if (core.state.syncTimer) { window.clearTimeout(core.state.syncTimer); core.state.syncTimer = null; }
  });

  const openConversation = async (title) => {
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${title}") .conversation-open`);
    await page.waitForTimeout(1200);
    await settlePollingOff();
  };

  const push = (event) => page.evaluate(
    (payload) => { window.__streams[window.__streams.length - 1].push(payload); },
    event,
  );

  try {
    await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 30000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 30000 });
    await settlePollingOff();

    // ─────────── A. 运行中：入口可用性 ───────────
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${ACTIVE_TITLE}") .conversation-open`);
    await page.waitForSelector('#messages.conversation-running', { timeout: 20000 });
    await page.waitForTimeout(800);
    await settlePollingOff();

    let seen = await availabilitySnapshot(page);
    check('A① 运行中：插话入口出现、输入框仍可用（有字才可点）',
      !seen.interjectHidden && seen.interjectDisabled
      && !seen.inputDisabled && seen.placeholder.includes('插话队列')
      && seen.sendTitle.includes('停止'),
      JSON.stringify(seen));
    let panel = await panelSnapshot(page);
    check('A② 本会话没有残留插话：队列面板整体隐藏（不是空面板）',
      panel.hidden === true && panel.rows.length === 0, JSON.stringify(panel));

    await page.fill('#messageInput', '改用竖版，先别截图');
    await page.waitForTimeout(200);
    seen = await availabilitySnapshot(page);
    check('A③ 打字后插话按钮可点', !seen.interjectDisabled, JSON.stringify(seen));
    await page.screenshot({ path: path.join(ROOT, 'verify', 'interjection_smoke_1_running.png') });

    // ─────────── B. 入队（真 POST 落库）───────────
    await page.press('#messageInput', 'Enter');
    await page.waitForTimeout(900);
    panel = await panelSnapshot(page);
    const first = rowByText(panel, '改用竖版');
    check('B① 回车入队：面板出现该行（待引导 + 四个动作）',
      Boolean(first) && first.state === 'pending' && first.badge === '待引导'
      && ['edit', 'delete', 'guide', 'reuse'].every((key) => first.actions.includes(key)),
      JSON.stringify(panel));
    check('B① 入队后输入框已清空、按钮回到不可点',
      (await page.inputValue('#messageInput')) === ''
      && (await availabilitySnapshot(page)).interjectDisabled,
      JSON.stringify(await availabilitySnapshot(page)));

    let messages = await messagesOf(ACTIVE_ID);
    const savedFirst = messages.find((m) => m.content === '改用竖版，先别截图');
    check('B② 已真落库：role=user + interjection 标记 + 未引导未消费',
      Boolean(savedFirst) && savedFirst.role === 'user'
      && savedFirst.metadata?.interjection === true
      && !savedFirst.metadata?.interjection_guided
      && !savedFirst.metadata?.interjection_consumed
      && String(savedFirst.metadata?.run_id || '') === RUN_ID,
      JSON.stringify(savedFirst && savedFirst.metadata));

    // ─────────── C. 编辑（就地改，id 不变）───────────
    await page.click(`#runGuidanceList .run-guidance-card[data-message-id="${savedFirst.id}"] [data-interjection-edit]`);
    await page.waitForTimeout(300);
    panel = await panelSnapshot(page);
    check('C① 点编辑就地展开输入框（不搬走底部输入区）',
      panel.rows.some((row) => row.id === savedFirst.id && row.editing)
      && (await page.inputValue('#messageInput')) === '',
      JSON.stringify(panel.rows));

    await page.fill('#runGuidanceList .run-guidance-input', '改用竖版 4K，先别截图');
    await page.press('#runGuidanceList .run-guidance-input', 'Enter');
    await page.waitForTimeout(900);
    panel = await panelSnapshot(page);
    const edited = panel.rows.find((row) => row.id === savedFirst.id);
    check('C② 保存后行内文本更新、状态仍待引导',
      Boolean(edited) && edited.text === '改用竖版 4K，先别截图' && edited.state === 'pending',
      JSON.stringify(edited));

    messages = await messagesOf(ACTIVE_ID);
    const stillOne = messages.filter((m) => m.metadata?.interjection);
    check('C③ 编辑是就地改：消息 id 稳定、不新增行、库里内容同步',
      stillOne.length === 1 && stillOne[0].id === savedFirst.id
      && stillOne[0].content === '改用竖版 4K，先别截图',
      JSON.stringify(stillOne.map((m) => [m.id, m.content])));

    // ─────────── D. 引导（agent 下一步取走）───────────
    await page.click(`#runGuidanceList .run-guidance-card[data-message-id="${savedFirst.id}"] [data-interjection-guide]`);
    await page.waitForTimeout(900);
    panel = await panelSnapshot(page);
    const guided = panel.rows.find((row) => row.id === savedFirst.id);
    check('D① 点引导：行转「已引导」且动作清空（不会再被误删/重复引导）',
      Boolean(guided) && guided.state === 'guided' && guided.badge === '已引导'
      && guided.actions.length === 0,
      JSON.stringify(guided));
    check('D② 面板抬头跟着改（不再谎报「N 条待引导」）',
      panel.head.includes('无可引导项'), panel.head);
    messages = await messagesOf(ACTIVE_ID);
    const guidedRow = messages.find((m) => m.id === savedFirst.id);
    check('D③ 后端已标记引导（等待 agent 取走）',
      guidedRow?.metadata?.interjection_guided === true
      && !guidedRow?.metadata?.interjection_consumed,
      JSON.stringify(guidedRow?.metadata));

    // ─────────── E. 再入队一条并删除 ───────────
    await page.fill('#messageInput', '这条待会儿删掉');
    await page.press('#messageInput', 'Enter');
    await page.waitForTimeout(900);
    panel = await panelSnapshot(page);
    const doomed = rowByText(panel, '这条待会儿删掉');
    check('E① 第二条入队成功', Boolean(doomed) && doomed.state === 'pending', JSON.stringify(panel.rows));

    await page.click(`#runGuidanceList .run-guidance-card[data-message-id="${doomed?.id || ''}"] [data-interjection-delete]`);
    await page.waitForTimeout(900);
    panel = await panelSnapshot(page);
    messages = await messagesOf(ACTIVE_ID);
    check('E② 删除：面板行消失 + 库里那条真没了（其余保留）',
      !rowByText(panel, '这条待会儿删掉')
      && !messages.some((m) => m.id === doomed.id)
      && messages.some((m) => m.id === savedFirst.id),
      JSON.stringify({ rows: panel.rows.map((row) => row.text), ids: messages.map((m) => m.id) }));

    // ─────────── F. Run 结束 → 队列冻结（只剩取回 / 删除）───────────
    // 前端口径：终态事件（done / cancelled / run_failed）都会调 freezeQueuedInterjections()，
    // 这里直接调那个导出函数；后端口径（`_finish` 落 stopped、stopped 行可删）另由单测守。
    await page.evaluate(async () => {
      const interj = await import('./js/18-interjections.js');
      const chat = await import('./js/12-chat-input.js');
      chat.setBusy(false);
      interj.freezeQueuedInterjections();
    });
    await page.waitForTimeout(400);
    panel = await panelSnapshot(page);
    const frozen = panel.rows.find((row) => row.id === savedFirst.id);
    check('F① 冻结后残留行转「已停止」，引导按钮消失、取回/删除可用',
      Boolean(frozen) && frozen.state === 'stopped' && frozen.badge === '已停止'
      && !frozen.actions.includes('guide')
      && frozen.actions.includes('reuse') && frozen.actions.includes('delete'),
      JSON.stringify(frozen));
    check('F② 冻结同时改抬头计数（不再有「待引导」可找）',
      panel.head.includes('无可引导项'), panel.head);
    check('F③ 队列冻结不影响输入区：输入框可用、发送键回到发送',
      await page.evaluate(() => {
        const input = document.querySelector('#messageInput');
        return !input.disabled && !document.querySelector('#sendButton').title.includes('停止');
      }), '');
    await page.screenshot({ path: path.join(ROOT, 'verify', 'interjection_smoke_2_frozen.png') });

    // ─────────── G. 重连恢复后不得滞留「重连中…」 ───────────
    await page.evaluate(async () => {
      const chat = await import('./js/12-chat-input.js');
      chat.setBusy(true);
    });
    await push({ type: 'status', message: '正在思考' });
    await page.waitForTimeout(400);
    seen = await availabilitySnapshot(page);
    check('G① 思考等待计时照旧（设计特性：正在思考 · 已等待 X 秒）',
      seen.status.includes('正在思考') && seen.status.includes('已等待'), seen.status);

    // 模拟一次断线重连（真实现里由 scheduleRunReconnect / enterReconnectCoolDown 触发）
    await page.evaluate(async () => {
      const run = await import('./js/11-run-stream.js');
      run.showElapsedStatus('重连中…', 'reconnect');
    });
    await page.waitForTimeout(300);
    seen = await availabilitySnapshot(page);
    check('G② 重连中状态可见（复现问题现场）',
      seen.status.includes('重连中') && seen.status.includes('已等待'), seen.status);

    // 重连恢复后只来了 reasoning_delta（不清计时的那几类事件之一）→ 必须自行撤销滞留。
    // 关键口径：计时器每秒都会把「重连中… · 已等待 X 秒」重新写上状态栏，所以只测 600ms
    // 不够——必须跨过一个刷新周期再看，那才证明计时器真的被清了，而不是被谁覆盖了一下。
    await push({ type: 'reasoning_delta', content: '嗯' });
    await page.waitForTimeout(1600);
    seen = await availabilitySnapshot(page);
    check('G③ 内容一到，「重连中…」立即消失且不再回来（跨过计时刷新周期）',
      !seen.status.includes('重连中'), `${seen.status} | ${JSON.stringify(await diag(page))}`);

    // 负向：不得把「正在思考 · 已等待 X 秒」也一起清掉（那是设计特性）
    await push({ type: 'status', message: '正在思考（第 2 轮）' });
    await page.waitForTimeout(300);
    await push({ type: 'reasoning_delta', content: '再' });
    await page.waitForTimeout(600);
    seen = await availabilitySnapshot(page);
    check('G④ 负向：reasoning_delta 不得清掉「正在思考」计时',
      seen.status.includes('正在思考') && seen.status.includes('已等待'), seen.status);

    // 重连成功这一侧：流断开 → 退避重连 → 新流建立即撤销滞留（本地模型 prefill 期无事件也成立）
    // 把「建流」故意拖慢：否则横幅一来就走，G⑤/G⑥ 都变成抢时序的脆弱断言。
    const before = await page.evaluate(() => {
      window.__eventsOpenDelay = 1200;
      return window.__streams.length;
    });
    await page.evaluate(() => { window.__streams[window.__streams.length - 1].fail(); });
    await page.waitForTimeout(900);
    seen = await availabilitySnapshot(page);
    check('G⑤ 断流后进入「重连中…」（重连计时照旧工作）',
      seen.status.includes('重连中'), `${seen.status} | ${JSON.stringify(await diag(page))}`);

    const reconnected = await page.waitForFunction(
      (count) => window.__streams.length > count && !window.__streams[window.__streams.length - 1].closed,
      before, { timeout: 20000 },
    ).then(() => true).catch(() => false);
    await page.waitForTimeout(900);
    seen = await availabilitySnapshot(page);
    check('G⑥ 重连成功即撤销「重连中…」（不等第一个内容事件）',
      reconnected && !seen.status.includes('重连中'),
      `${JSON.stringify(seen)} reconnected=${reconnected} | ${JSON.stringify(await diag(page))}`);
    await page.screenshot({ path: path.join(ROOT, 'verify', 'interjection_smoke_3_reconnected.png') });

    // ─────────── H. 与选择面板共存：两条通道互不锁死 ───────────
    await openConversation(CHOICE_TITLE);
    await page.waitForSelector('#choiceButtons button[data-choice-value]', { timeout: 20000 });
    await page.waitForTimeout(600);
    seen = await availabilitySnapshot(page);
    panel = await panelSnapshot(page);
    const leftover = rowByText(panel, '冻结的残留插话');
    check('H① 选择题面板与插话面板同屏共存，且都没被对方锁住',
      seen.choiceButtons > 0 && seen.choiceDisabled === 0
      && !seen.inputDisabled && Boolean(leftover),
      JSON.stringify({ ...seen, rows: panel.rows }));
    check('H② 上一轮冻结的残留插话照旧可见（可清理、不会被静默丢弃）',
      leftover.state === 'stopped' && leftover.actions.includes('reuse')
      && leftover.actions.includes('delete') && !leftover.actions.includes('guide'),
      JSON.stringify(leftover));
    check('H③ 未消费的插话不进消息流（只由队列面板承载）',
      await page.evaluate(() => ![...document.querySelectorAll('#messages .message-body, #messages .message-row')]
        .some((node) => node.textContent.includes('冻结的残留插话'))), '');

    // 「取回输入框」：run 结束后残留插话的唯一出口（写回输入框 + 真删掉那一行）
    const CHOICE_ID = process.env.NAIBA_SMOKE_CHOICE_ID || '';
    await page.click('#runGuidanceList [data-interjection-reuse]');
    await page.waitForTimeout(900);
    panel = await panelSnapshot(page);
    const reused = await page.inputValue('#messageInput');
    const choiceMessages = await messagesOf(CHOICE_ID);
    check('H④ 取回：正文回到输入框、面板行消失、库里那条真删了',
      reused.includes('冻结的残留插话')
      && !rowByText(panel, '冻结的残留插话')
      && !choiceMessages.some((m) => m.metadata?.interjection),
      JSON.stringify({ reused, rows: panel.rows.length }));
    check('H⑤ 取回后仍是空闲态：输入框可用、选择面板未被带进运行态',
      await page.evaluate(() => {
        const input = document.querySelector('#messageInput');
        const host = document.querySelector('#choiceButtons');
        return !input.disabled && !host.classList.contains('is-busy')
          && [...host.querySelectorAll('button[data-choice-value]')].every((b) => !b.disabled);
      }), '');

    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
