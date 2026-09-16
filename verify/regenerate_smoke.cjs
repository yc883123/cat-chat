// 「重新生成」+「编辑」回归冒烟（由 verify/regenerate_smoke.py 起独立源码 server 后调用）。
//
// 覆盖（全部走真实前端 + 真实后端截断，只用假流挡住模型调用）：
//   ① 按钮就位与顺序：用户消息 编辑 → 分支；AI 回复 复制 → 重新生成 → 新会话；
//   ② 中间轮点「重新生成」必须弹确认（含剩余条数 + 草稿提示），取消则一切不变；
//   ③ 最后一轮点「重新生成」不弹确认，**截断点是那条提问**，重发的是同一条提问原文，
//      且前缀里没有原答复、没有重复提问；
//   ④ 「编辑」一条**富消息**（/ref 技能引用 + @ 工作区引用 + 图片附件）：
//      编辑框逐字回填、原附件可见 → 改字 → 点**底部发送按钮**确认 → 三条引用链都得活着：
//        · display_message = 用户原样（/ref 与 @ 逐字保留）
//        · message 已剥离 /ref，但 @ 仍在（后端才解析成绝对路径）
//        · attachments 带回图片的 path 与 thumb_path（不是只有 path）
//      并且前缀（更早的消息）逐字保留、后端只掉被截断的那一段；
//   ⑤ 编辑中撞上「同会话刷新」（拦截会话详情接口造出真实的一次 syncCurrentConversation）：
//      编辑框仍在原气泡、改到一半的字原样保留、发送键仍是「重新发送」——改前这里会静默退编辑态；
//   ⑥ 编辑中被截断/删除（被编辑的消息从数据里消失）：退出编辑但**底部草稿逐字还回来** + toast 告知；
//   ⑦ 编辑最前面那条提问 → 后端清空（截断到头的边界）；
//   ⑧ 零页面错误。
//
// 行定位一律用 message id（`rowSel()`）：进入编辑后正文被编辑框替换，按 `:has-text(原文)` 找行
// 会在清空编辑框后假失败（镜像层也变空）。
//
// 假 /api/chat：只回一段最小 NDJSON，让 UI 落定；真实的前缀缓存在后端，
// 由「后端剩余消息 == 预期前缀」来断言（build_model_history 对存储消息 1:1 映射）。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8796';
const BOUND_MODEL = process.env.NAIBA_SMOKE_BOUND_MODEL || 'smoke-pro';
const Q1 = process.env.NAIBA_SMOKE_Q1 || '重新生成冒烟：第一问';
const A1 = process.env.NAIBA_SMOKE_A1 || '第一答：冒烟历史。';
const A2 = process.env.NAIBA_SMOKE_A2 || '第二答：继续的回复。';
const A3 = process.env.NAIBA_SMOKE_A3 || '第三答：收尾的回复。';
const Q2_PLAIN = process.env.NAIBA_SMOKE_Q2_PLAIN || '第二问：继续';
const Q2_DISPLAY = process.env.NAIBA_SMOKE_Q2_DISPLAY || Q2_PLAIN;
const Q2_CONTENT = process.env.NAIBA_SMOKE_Q2_CONTENT || Q2_PLAIN;
const Q3_PLAIN = process.env.NAIBA_SMOKE_Q3_PLAIN || '第三问：收尾';
const Q3_DISPLAY = process.env.NAIBA_SMOKE_Q3_DISPLAY || Q3_PLAIN;
const AT_REF = process.env.NAIBA_SMOKE_AT_REF || '';
const IMG_PATH = process.env.NAIBA_SMOKE_IMG_PATH || '';
const IMG_THUMB = process.env.NAIBA_SMOKE_IMG_THUMB || '';
// 第二问里那个 /ref 引用（display 去掉 @ 引用与正文后的那段）。
const SKILL_REF = Q2_DISPLAY.replace(Q2_PLAIN, '').replace(`@${AT_REF}`, '').trim();
const EDITED = '第一问：改过之后的问题';
const EDITED_RICH = '第二问：改过之后的问题（带引用重发）';
// 编辑态保护（P1）：编辑到一半的内容 / 进入编辑前的底部草稿 / 同步里冒出来的后台消息。
const EDITED_INLINE = '改到一半的编辑内容';
const DRAFT_KEEP = '底部草稿：编辑期间别把它吞掉';
const DRAFT_RESTORE = '底部草稿：被编辑的消息没了也别丢';
const SYNC_MSG = '后台任务写入的一条消息';

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(path, options = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(`${BASE}${path}`, init);
  return response.json().catch(() => ({}));
}

async function messagesOf(conversationId) {
  const data = await apiJson(`/api/conversations/${encodeURIComponent(conversationId)}`);
  return data.messages || [];
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  // 前端自带 `[naiba] renderMessages 调用` 诊断日志：哪一步引爆了重渲染，看这个最直接。
  const naibaLogs = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    const text = msg.text();
    if (text.includes('[naiba]')) naibaLogs.push(text);
    if (msg.type() === 'error') pageErrors.push(`console.error: ${text}`);
  });
  // 确认框统一「取消」：只有明确要执行的步骤才需要它不出现/被接受。
  const dialogs = [];
  page.on('dialog', async (dialog) => {
    dialogs.push(dialog.message());
    await dialog.dismiss().catch(() => {});
  });

  // 目录请求拦截：让「会话底部选择到已检测模型」这一发送前置条件成立（不真连供应商）。
  await page.route('**/api/providers/models', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ models: [{ id: BOUND_MODEL, name: BOUND_MODEL }, { id: 'smoke-flash', name: 'smoke-flash' }] }),
  }));

  // /api/chat 拦截：记录请求体，回一段最小 NDJSON 让 UI 落定（不真调模型）。
  const chatPayloads = [];
  await page.route('**/api/chat', async (route) => {
    let body = {};
    try { body = route.request().postDataJSON() || {}; } catch (_) { body = {}; }
    chatPayloads.push(body);
    const runId = `smoke-run-${chatPayloads.length}`;
    const stream = [
      { type: 'run_started', run_id: runId, sequence: 1 },
      { type: 'delta', run_id: runId, content: '冒烟答复', sequence: 2 },
      {
        type: 'done',
        run_id: runId,
        sequence: 3,
        message: {
          id: `smoke-assistant-${chatPayloads.length}`,
          role: 'assistant',
          content: '冒烟答复',
          created_at: Date.now(),
          metadata: {},
        },
      },
    ].map((event) => JSON.stringify(event)).join('\n') + '\n';
    await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: stream });
  });

  // 会话详情接口拦截：造出**真实的一次同会话同步**会看到的快照变化
  // （syncCurrentConversation 只在 updated_at|条数|末条 id|末条 role 变化时才重渲染）。
  //   { mode:'append' }         → 多一条消息（快照变了，被编辑的消息还在 → 应保现场）
  //   { mode:'drop', messageId } → 少一条消息（编辑中的那条没了 → 应退出编辑并还草稿）
  let injectSync = null;
  await page.route('**/api/conversations/*', async (route) => {
    if (route.request().method() !== 'GET' || !injectSync) return route.continue();
    const response = await route.fetch();
    const data = await response.json().catch(() => ({}));
    const messages = Array.isArray(data.messages) ? data.messages.slice() : [];
    data.messages = injectSync.mode === 'append'
      ? [...messages, { id: injectSync.id, role: 'assistant', content: injectSync.content, created_at: Date.now(), metadata: {} }]
      : messages.filter((message) => String(message.id) !== String(injectSync.messageId));
    await route.fulfill({ response, json: data });
  });

  // 触发一次真实同步：startConversationSync 监听 visibilitychange → 立即 scheduleConversationSync(0)。
  const triggerSync = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));

  async function waitFor(fn, timeout = 15000, step = 120) {
    const end = Date.now() + timeout;
    let last = null;
    while (Date.now() < end) {
      last = await fn();
      if (last) return last;
      await page.waitForTimeout(step);
    }
    return last;
  }

  // 行定位一律用 message id，不用 `:has-text(原文)`：进入编辑后正文会被编辑框替换
  // （清空编辑框时连镜像层也只剩零宽字符），按文本找行会在半路假失败。
  async function messageIdOf(text) {
    return page.evaluate((wanted) => {
      const row = [...document.querySelectorAll('#messages .message-row[data-message-id]')]
        .find((el) => el.textContent.includes(wanted));
      return row ? row.dataset.messageId : '';
    }, text);
  }
  const rowSel = (id) => `#messages .message-row[data-message-id="${id}"]`;

  // 页面重载 + 等消息渲染：一次「发送」之后 state.messages 里会留下乐观行（乐观 user 行没有 id、
  // 假流回的 assistant 行 id 是 smoke-assistant-N），它们会污染后续「剩余条数」的推导，
  // 所以每个阶段之间重载一次，让 DOM/state 全部回到后端真值。
  async function reload() {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 25000 });
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 25000 });
    await page.waitForTimeout(600);
  }

  const actionSnapshot = () => page.evaluate(() => {
    const rows = [...document.querySelectorAll('#messages .message-row[data-message-id]')];
    return rows.map((row) => {
      const regen = row.querySelector('[data-regenerate-message]');
      return {
        role: row.classList.contains('user') ? 'user' : 'assistant',
        buttons: [...row.querySelectorAll('.message-actions button')].map((b) => b.textContent.trim()),
        regenShown: regen ? getComputedStyle(regen).display !== 'none' : null,
      };
    });
  });

  const bridge = () => page.evaluate(() => {
    const btn = document.querySelector('#sendButton');
    const input = document.querySelector('#messageInput');
    return {
      editingClass: document.body.classList.contains('is-editing-message'),
      inputDisabled: Boolean(input && input.disabled),
      placeholder: input ? input.placeholder : '',
      attachDisabled: Boolean(document.querySelector('#attachButton')?.disabled),
      sendTitle: btn ? btn.title : '',
      sendDisabled: btn ? btn.disabled : null,
    };
  });

  try {
    await reload();

    // 会话 ID 从接口拿：只有一个会话，直接取列表第一项。
    const list = await apiJson('/api/conversations');
    const convId = String(((list.conversations || [])[0] || {}).id || '');
    check('拿到冒烟会话 ID', Boolean(convId), convId);

    // ---- ① 按钮就位与顺序 ----
    const snap = await actionSnapshot();
    check('初始 6 条消息（三轮问答）', snap.length === 6, JSON.stringify(snap.map((s) => s.role)));
    const userRows = snap.filter((s) => s.role === 'user');
    const aiRows = snap.filter((s) => s.role === 'assistant');
    check('用户消息操作区 = 编辑 → 分支',
      userRows.length === 3 && userRows.every((s) => s.buttons.join(',') === '编辑,分支'),
      JSON.stringify(userRows.map((s) => s.buttons)));
    check('AI 回复操作区 = 复制 → 重新生成 → 新会话',
      aiRows.length === 3 && aiRows.every((s) => s.buttons.join(',') === '复制,重新生成,新会话'),
      JSON.stringify(aiRows.map((s) => s.buttons)));
    check('「重新生成」按钮在空闲态可见', aiRows.every((s) => s.regenShown === true),
      JSON.stringify(aiRows.map((s) => s.regenShown)));
    check('富消息的图片附件渲染出来了（图片引用没在路上丢）',
      (await page.locator(`#messages .message-row:has-text("${Q2_PLAIN}") figure.attachment-image`).count()) === 1);

    // 留一张真实截图，便于人工肉眼确认按钮就位（失败时也照样留，用于排查）。
    await page.screenshot({
      path: process.env.NAIBA_SMOKE_SHOT
        || require('path').join(__dirname, 'regenerate_smoke_actions.png'),
      fullPage: false,
    });

    // ---- ② 中间轮：弹确认（含剩余条数 + 草稿提示），取消后一切不变 ----
    dialogs.length = 0;
    await page.fill('#messageInput', '这是一段草稿');
    await page.click(`#messages .message-row:has-text("${A2}") [data-regenerate-message]`);
    await waitFor(() => (dialogs.length ? true : null), 6000);
    check('中间轮「重新生成」弹确认框', dialogs.length === 1, JSON.stringify(dialogs));
    check('确认文案标出会被删除的条数', Boolean(dialogs[0] && dialogs[0].includes('还有 2 条消息')), dialogs[0]);
    check('确认文案标出草稿会被替换', Boolean(dialogs[0] && dialogs[0].includes('输入框里的草稿会被替换成这条提问')), dialogs[0]);
    check('取消后不发任何请求', chatPayloads.length === 0, String(chatPayloads.length));
    check('取消后消息数不变（6 条）', (await messagesOf(convId)).length === 6);
    await page.fill('#messageInput', '');

    // ---- ③ 最后一轮：不弹确认；截断点是那条提问 ----
    dialogs.length = 0;
    await page.click(`#messages .message-row:has-text("${A3}") [data-regenerate-message]`);
    const p1 = await waitFor(() => chatPayloads[0] || null);
    check('最后一轮「重新生成」不弹确认框', dialogs.length === 0, JSON.stringify(dialogs));
    check('发起了重发（一次 /api/chat）', Boolean(p1));
    check('display_message 是用户原样（/ref 引用逐字保留）',
      Boolean(p1) && p1.display_message === Q3_DISPLAY, String(p1 && p1.display_message));
    check('发给模型的正文已剥离 /ref（引用不当正文塞给模型）',
      Boolean(p1) && !String(p1.message).includes(SKILL_REF) && String(p1.message).trim() === Q3_PLAIN,
      JSON.stringify({ message: p1 && p1.message, ref: SKILL_REF }));
    check('用的是当前顶栏选中的模型', p1 && p1.model_name === BOUND_MODEL, String(p1 && p1.model_name));
    check('重发针对当前会话', p1 && p1.conversation_id === convId, String(p1 && p1.conversation_id));
    const after3 = await messagesOf(convId);
    check('截断点 = 那条提问：后端只剩前两轮（4 条）',
      after3.length === 4, JSON.stringify(after3.map((m) => `${m.role}:${String(m.content).slice(0, 10)}`)));
    check('U1,A1 逐字节保留（前缀缓存命中的前提）',
      after3[0] && after3[0].content === Q1 && after3[1] && after3[1].content === A1,
      JSON.stringify(after3.map((m) => String(m.content))));
    check('原答复已从历史移除（不会重复提问/答复）',
      !JSON.stringify(after3).includes(A3), JSON.stringify(after3.map((m) => String(m.content))));

    await reload();

    // ---- ④ 「编辑」富消息：完整 composer 移入气泡，附件可删/取消可恢复 ----
    dialogs.length = 0;
    const before = chatPayloads.length;
    const q2Id = await messageIdOf(Q2_PLAIN);
    check('拿到富消息（第二问）的行 id', Boolean(q2Id), q2Id);
    await page.click(`${rowSel(q2Id)} [data-edit-message]`);
    await page.waitForSelector(`${rowSel(q2Id)} .composer-wrap`, { timeout: 10000 });
    await page.waitForTimeout(300);
    const prefilled = await page.inputValue(`${rowSel(q2Id)} #messageInput`);
    check('「编辑」逐字回填用户原文（含 /ref 与 @ 工作区引用）', prefilled === Q2_DISPLAY, prefilled);
    check('编辑框里带出了原图片附件（不是只留文字）',
      (await page.locator(`${rowSel(q2Id)} #pendingFiles .pending-item`).count()) === 1);

    const placement = await page.evaluate((id) => {
      const row = document.querySelector(`#messages .message-row[data-message-id="${id}"]`);
      const inline = row?.querySelector('.composer-wrap');
      const bottom = [...document.querySelectorAll('.composer-wrap')].find((el) => !row?.contains(el));
      return {
        inline: Boolean(inline),
        inputInRow: Boolean(inline?.querySelector('#messageInput')),
        bottomHidden: Boolean(bottom && (bottom.hidden || getComputedStyle(bottom).display === 'none')),
        uniqueInput: document.querySelectorAll('#messageInput').length === 1,
      };
    }, q2Id);
    check('完整 composer-wrap 已移动到选择编辑的消息气泡',
      placement.inline && placement.inputInRow && placement.uniqueInput, JSON.stringify(placement));
    check('底部 composer 隐藏并保留占位', placement.bottomHidden, JSON.stringify(placement));

    const b1 = await bridge();
    check('编辑态已标记（body.is-editing-message）', b1.editingClass === true, JSON.stringify(b1));
    check('移动后的输入框保持可编辑', b1.inputDisabled === false, JSON.stringify(b1));
    check('移动后的添加文件按钮保持可用', b1.attachDisabled === false, JSON.stringify(b1));
    check('发送按钮变成「重新发送」且可用',
      b1.sendDisabled === false && b1.sendTitle.includes('重新发送'), JSON.stringify(b1));

    // 留一张编辑态截图：底部输入区让位 + 编辑框里的附件 + 发送键变「重新发送」。
    await page.screenshot({
      path: process.env.NAIBA_SMOKE_SHOT_EDIT
        || require('path').join(__dirname, 'regenerate_smoke_editing.png'),
      fullPage: false,
    });

    // 原附件位于移动后的 pendingFiles，点移除应立即消失；取消编辑后原附件恢复。
    await page.click(`${rowSel(q2Id)} #pendingFiles [data-remove-file]`);
    check('编辑态可删除原消息附件',
      (await page.locator(`${rowSel(q2Id)} #pendingFiles .pending-item`).count()) === 0);
    await page.click(`${rowSel(q2Id)} [data-edit-cancel]`);
    await page.waitForTimeout(250);
    check('取消编辑后底部 composer 恢复',
      (await page.locator('.composer-wrap #messageInput').count()) === 1);
    await page.click(`${rowSel(q2Id)} [data-edit-message]`);
    await page.waitForSelector(`${rowSel(q2Id)} .composer-wrap`);
    check('取消后重新编辑仍恢复原附件',
      (await page.locator(`${rowSel(q2Id)} #pendingFiles .pending-item`).count()) === 1);

    // 清空编辑框 → 底部按钮可用性跟着编辑框走（纯附件轮次：有附件仍应可发）
    await page.fill(`${rowSel(q2Id)} #messageInput`, '');
    await page.waitForTimeout(200);
    const b2 = await bridge();
    check('编辑框清空但仍有图片附件时「重新发送」保持可用', b2.sendDisabled === false, JSON.stringify(b2));

    // 改字后点**底部发送按钮**（不是编辑框里的按钮）→ 必须确认编辑，而不是发新消息
    await page.fill(`${rowSel(q2Id)} #messageInput`, EDITED_RICH);
    await page.click(`${rowSel(q2Id)} #pendingFiles [data-remove-file]`);
    await page.waitForTimeout(200);
    await page.click('#sendButton');
    const p2 = await waitFor(() => chatPayloads[before] || null);
    check('底部发送按钮确认了编辑（重发改后文本）', p2 && p2.message === EDITED_RICH, String(p2 && p2.message));
    check('重发的 display_message 就是编辑框里的文本', p2 && p2.display_message === EDITED_RICH,
      String(p2 && p2.display_message));
    check('重发使用编辑后保留的附件（已删除则数量为 0）',
      Boolean(p2) && Array.isArray(p2.attachments) && p2.attachments.length === 0,
      JSON.stringify(p2 && p2.attachments));
    const after4 = await messagesOf(convId);
    check('「编辑」从该提问截断：后端只剩更早的两条（U1,A1）',
      after4.length === 2, JSON.stringify(after4.map((m) => `${m.role}:${String(m.content).slice(0, 12)}`)));
    check('截断点之前的消息逐字节保留（改后文的这一轮不污染前缀）',
      after4[0] && after4[0].content === Q1 && after4[1] && after4[1].content === A1,
      JSON.stringify(after4.map((m) => String(m.content))));
    const b3 = await bridge();
    check('确认后编辑态已退出（底部恢复常态）',
      b3.editingClass === false && !b3.placeholder.includes('正在编辑'), JSON.stringify(b3));

    await reload();

    // ---- ⑤ 编辑中撞上「同会话刷新」：编辑现场必须原样活着 ----
    // 轮询 syncCurrentConversation（后台任务写消息、流式落库都会引爆）与重新打开当前会话都会走
    // renderMessages；改前那里无条件 applyEditingState(null) → 编辑被静默取消、改到一半的字掉回
    // 底部输入框、发送键从「重新发送」变回「发送」。这里用拦截会话详情接口造出真实的一次同步。
    const u1Id = await messageIdOf(Q1);
    await page.fill('#messageInput', DRAFT_KEEP);
    await page.click(`${rowSel(u1Id)} [data-edit-message]`);
    await page.waitForSelector(`${rowSel(u1Id)} #messageInput`, { timeout: 10000 });
    await page.fill(`${rowSel(u1Id)} #messageInput`, EDITED_INLINE);
    await page.waitForTimeout(150);
    injectSync = { mode: 'append', id: 'smoke-sync-1', content: SYNC_MSG };
    await triggerSync();
    const rerendered = await waitFor(() => page.evaluate((text) => (
      [...document.querySelectorAll('#messages .message-row')].some((row) => row.textContent.includes(text))
        ? 'rendered' : null
    ), SYNC_MSG), 10000);
    check('同会话刷新确实发生了（注入的新消息被渲染出来）', rerendered === 'rendered', String(rerendered));
    injectSync = null;
    const surv = await page.evaluate((id) => {
      const row = document.querySelector(`#messages .message-row[data-message-id="${id}"]`);
      const input = document.querySelector('#messageInput');
      const btn = document.querySelector('#sendButton');
      return {
        inRow: Boolean(row && input && row.contains(input)),
        uniqueInput: document.querySelectorAll('#messageInput').length === 1,
        value: input ? input.value : null,
        editingRows: document.querySelectorAll('#messages .message-row.is-editing').length,
        isEditingRow: Boolean(row && row.classList.contains('is-editing')),
        pending: row ? row.querySelectorAll('#pendingFiles .pending-item').length : -1,
        bodyEditing: document.body.classList.contains('is-editing-message'),
        sendTitle: btn ? btn.title : '',
        sendDisabled: btn ? btn.disabled : null,
      };
    }, u1Id);
    check('刷新后编辑框仍在原来那条消息气泡里（唯一输入框）',
      surv.inRow && surv.uniqueInput && surv.isEditingRow && surv.editingRows === 1, JSON.stringify(surv));
    check('改到一半的编辑内容原样保留', surv.value === EDITED_INLINE, JSON.stringify(surv));
    check('编辑态标记未被打断（仍标着 is-editing-message + 发送键还是「重新发送」）',
      surv.bodyEditing && surv.sendTitle.includes('重新发送') && surv.sendDisabled === false, JSON.stringify(surv));
    await page.screenshot({
      path: process.env.NAIBA_SMOKE_SHOT_EDIT_SYNC
        || require('path').join(__dirname, 'regenerate_smoke_edit_survives_sync.png'),
      fullPage: false,
    });
    // 复挂不能弄脏 stash：取消后底部草稿必须原样回来。
    await page.click(`${rowSel(u1Id)} [data-edit-cancel]`);
    const draftBack = await waitFor(() => page.evaluate((draft) => {
      const input = document.querySelector('#messageInput');
      if (!input || input.closest('.message-row')) return null;
      return input.value === draft ? input.value : null;
    }, DRAFT_KEEP), 8000);
    check('复挂没有弄脏底部草稿（取消后逐字还原）', draftBack === DRAFT_KEEP, String(draftBack));

    // ---- ⑥ 编辑中被截断/删除：退出编辑，但草稿必须还回来（旧行为是静默丢弃）----
    await reload();
    const goneId = await messageIdOf(Q1);
    await page.fill('#messageInput', DRAFT_RESTORE);
    await page.click(`${rowSel(goneId)} [data-edit-message]`);
    await page.waitForSelector(`${rowSel(goneId)} #messageInput`, { timeout: 10000 });
    await page.fill(`${rowSel(goneId)} #messageInput`, '编辑到一半这条提问就没了');
    await page.waitForTimeout(150);
    injectSync = { mode: 'drop', messageId: goneId };
    await triggerSync();
    const dropped = await waitFor(() => page.evaluate((id) => (
      document.querySelector(`#messages .message-row[data-message-id="${id}"]`) ? null : 'dropped'
    ), goneId), 10000);
    check('被编辑的消息已从数据里消失（同步已生效）', dropped === 'dropped', String(dropped));
    const exited = await page.evaluate(() => {
      const input = document.querySelector('#messageInput');
      return {
        uniqueInput: document.querySelectorAll('#messageInput').length === 1,
        inRow: Boolean(input && input.closest('.message-row')),
        value: input ? input.value : '',
        bodyEditing: document.body.classList.contains('is-editing-message'),
        editingRows: document.querySelectorAll('#messages .message-row.is-editing').length,
        toast: document.querySelector('#toast')?.textContent || '',
      };
    });
    check('编辑态已退出、composer 回到最底部',
      exited.uniqueInput && !exited.inRow && exited.bodyEditing === false && exited.editingRows === 0,
      JSON.stringify(exited));
    check('底部草稿逐字还原（旧行为会静默丢弃）', exited.value === DRAFT_RESTORE, JSON.stringify(exited));
    check('明确告知用户「草稿已还原到底部输入框」',
      exited.toast.includes('草稿已还原到底部输入框'), exited.toast);
    await page.screenshot({
      path: process.env.NAIBA_SMOKE_SHOT_DRAFT
        || require('path').join(__dirname, 'regenerate_smoke_draft_restored.png'),
      fullPage: false,
    });
    injectSync = null;
    await reload();

    // ---- ⑦ 编辑最前面那条 → 后端清空（截断到头的边界）----
    const firstId = await messageIdOf(Q1);
    await page.click(`${rowSel(firstId)} [data-edit-message]`);
    await page.waitForSelector(`${rowSel(firstId)} #messageInput`, { timeout: 10000 });
    const p3before = chatPayloads.length;
    await page.fill(`${rowSel(firstId)} #messageInput`, EDITED);
    await page.waitForTimeout(200);
    await page.click('#sendButton');
    const p3 = await waitFor(() => chatPayloads[p3before] || null);
    check('编辑框内「重新发送」按钮生效', p3 && p3.message === EDITED, String(p3 && p3.message));
    check('编辑首条提问后后端清空', (await messagesOf(convId)).length === 0);

    // ---- ⑧ 零页面错误 ----
    check('页面无 JS 错误', pageErrors.length === 0, pageErrors.join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, error && error.message ? error.message : String(error));
    // 失败现场：编辑框在不在气泡里、唯一输入框落在哪、最近几次重渲染 —— 这三样能直接区分
    // 「编辑被静默取消」「composer 归位了但没复挂」「根本没进编辑态」。
    try {
      const dump = await page.evaluate(() => ({
        inputs: [...document.querySelectorAll('#messageInput')].map((el) => ({
          value: String(el.value || '').slice(0, 40),
          inRow: Boolean(el.closest('.message-row')),
          wrapClass: el.closest('.composer-wrap')?.className || '',
          hidden: Boolean(el.closest('.composer-wrap')?.hidden),
        })),
        rows: [...document.querySelectorAll('#messages .message-row')].map((row) => ({
          id: row.dataset.messageId || '',
          cls: row.className,
          text: (row.textContent || '').replace(/\s+/g, ' ').slice(0, 40),
          hasComposer: Boolean(row.querySelector('.composer-wrap')),
        })),
        mirror: (document.querySelector('#inputMirror')?.textContent || '').slice(0, 50),
        mirrorInWrap: Boolean(document.querySelector('.composer-wrap #inputMirror')),
        bodyClass: document.body.className,
        toast: document.querySelector('#toast')?.textContent || '',
      }));
      console.log(`\n[DEBUG 现场] ${JSON.stringify(dump, null, 2)}`);
      console.log(`[DEBUG naiba 日志] ${naibaLogs.slice(-6).join(' || ')}`);
      console.log(`[DEBUG 页面错误] ${pageErrors.join(' | ') || '（无）'}`);
    } catch (_) { /* 求值失败时忽略，至少保住原报错 */ }
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\n${failures.length} 项未通过` : '\n全部通过');
  process.exit(failures.length ? 1 : 0);
})();
