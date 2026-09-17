// Q1「本地模型上下文溢出的用户可见报错」冒烟（由 verify/q1_context_smoke.py 起隔离源码实例后调用）。
//
// 覆盖：
//   ① 静态形态：三条带 metadata.truncated 的助手回复 —— cause=context 的提示行必须说
//      「上下文窗口已耗尽 + 新会话」，cause=output 说「已达模型输出上限」，没有 cause 的老数据
//      仍按输出上限显示（向后兼容）；
//   ② 真链路：对绑定假本地后端的会话真发一条消息 → 真 run → 假后端回 llama.cpp 的
//      `exceed_context_size_error` 400 → 消息区必须出现**可读、可行动**的失败文案
//      （后端名 + 后端自报的真实窗口 + 「新会话」指引），且不得出现原始 JSON；
//   ③ 后端真值：失败消息与截断 metadata 都回读 `GET /api/conversations/<id>`，
//      DOM 只用来断言「用户看得见什么」；
//   ④ 零页面错误。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8803';
const RENDER_ID = process.env.NAIBA_SMOKE_RENDER_ID || '';
const FAIL_ID = process.env.NAIBA_SMOKE_FAIL_ID || '';
const FAIL_Q = process.env.NAIBA_SMOKE_FAIL_Q || '';
const PROVIDER_NAME = process.env.NAIBA_SMOKE_PROVIDER_NAME || '';
const BACKEND_WINDOW = process.env.NAIBA_SMOKE_BACKEND_WINDOW || '4096';
const SHOTS = process.env.NAIBA_SMOKE_SHOTS || require('path').join(__dirname, '..', 'verify');

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}
function shot(name) {
  return require('path').join(SHOTS, `q1_context_smoke_${name}.png`);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 940 } });
  const pageErrors = [];
  const httpErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`);
  });
  page.on('response', (response) => {
    if (response.status() >= 400) httpErrors.push(`${response.status()} ${response.url()}`);
  });

  const apiJson = async (path, options = {}) => {
    const init = { headers: { 'Content-Type': 'application/json' }, ...options };
    if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
    const response = await fetch(`${BASE}${path}`, init);
    return response.json().catch(() => ({}));
  };

  async function waitFor(fn, timeout = 20000, step = 120) {
    const end = Date.now() + timeout;
    let last = null;
    while (Date.now() < end) {
      last = await fn();
      if (last) return last;
      await page.waitForTimeout(step);
    }
    return last;
  }

  const rowOf = (id) => `.conversation-item[data-conversation-id="${id}"]`;
  // 按正文定位那一行，再读出它自己的截断提示行文本（不猜 DOM 顺序）。
  const noticeOf = (wanted) => page.evaluate((text) => {
    const row = [...document.querySelectorAll('#messages .message-row')].find((el) => {
      const answer = el.querySelector('.answer-content');
      return answer && (answer.textContent || '').includes(text);
    });
    if (!row) return null;
    const notice = row.querySelector('.truncation-notice');
    return { notice: notice ? (notice.textContent || '') : '', rowClass: row.className };
  }, wanted);

  try {
    // 供应商 base_url 指向本地假后端，不拦目录请求就会在控制台留下 404 噪音。
    await page.route('**/api/providers/models', (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ models: [{ id: 'smoke-local-q1', name: 'smoke-local-q1' }] }),
    }));

    await page.goto(BASE, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector(rowOf(RENDER_ID), { timeout: 20000 });
    await page.waitForTimeout(400);

    // ==================================================== ① 静态形态：提示行
    await page.click(rowOf(RENDER_ID));
    await page.waitForSelector('#messages .message-row', { timeout: 20000 });
    await waitFor(async () => (await page.locator('#messages .message-row').count()) >= 4, 15000);

    const notices = await page.locator('#messages .truncation-notice').count();
    check('三条截断自述各渲染出一条提示行', notices === 3, `实际 ${notices}`);

    const ctx = await noticeOf('撞窗口的那一轮回答');
    check('撞窗口那行提示「上下文窗口已耗尽」',
      Boolean(ctx) && ctx.notice.includes('上下文窗口已耗尽'), JSON.stringify(ctx));
    check('撞窗口那行提示指向新会话',
      Boolean(ctx) && ctx.notice.includes('新会话'), JSON.stringify(ctx));
    check('撞窗口那行保留「已自动续写一次」事实',
      Boolean(ctx) && ctx.notice.includes('已自动续写一次'), JSON.stringify(ctx));

    const out = await noticeOf('撞输出上限的那一轮回答');
    check('撞输出上限那行仍说「已达模型输出上限」',
      Boolean(out) && out.notice.includes('已达模型输出上限'), JSON.stringify(out));
    check('撞输出上限那行不得误报窗口耗尽',
      Boolean(out) && !out.notice.includes('上下文窗口耗尽'), JSON.stringify(out));

    const legacy = await noticeOf('老数据：只有 truncated 没有 cause');
    check('老数据（没有 cause）仍按输出上限显示（向后兼容）',
      Boolean(legacy) && legacy.notice.includes('已达模型输出上限'), JSON.stringify(legacy));

    // 后端真值：前端不是自己编的 cause，metadata 里本来就有。
    const renderDetail = await apiJson(`/api/conversations/${RENDER_ID}`);
    const renderMessages = renderDetail.messages || [];
    const renderTruncations = renderMessages
      .map((m) => (m.metadata || {}).truncated)
      .filter((t) => t && t.truncated);
    check('后端 metadata 里三条截断自述齐全', renderTruncations.length === 3,
      JSON.stringify(renderTruncations));
    check('后端 metadata 明确记着 cause=context',
      renderTruncations.some((t) => t.cause === 'context'), JSON.stringify(renderTruncations));
    await page.screenshot({ path: shot('1_notices') });

    // ==================================================== ② 真链路：真发送 → 真 400
    await page.click(rowOf(FAIL_ID));
    await page.waitForTimeout(500);
    await page.fill('#messageInput', FAIL_Q);
    await page.click('#sendButton');

    const failed = await waitFor(() => page.evaluate(() => {
      const row = [...document.querySelectorAll('#messages .message-row')].find((el) => {
        const answer = el.querySelector('.answer-content') || el.querySelector('.message-body');
        return answer && (answer.textContent || '').includes('上下文窗口不足');
      });
      return row ? { text: row.textContent || '', cls: row.className } : null;
    }), 30000, 200);
    check('消息区出现「上下文窗口不足」的失败文案', Boolean(failed), JSON.stringify(failed));
    const visible = (failed && failed.text) || '';
    check('失败文案点名后端', visible.includes(PROVIDER_NAME), visible.slice(0, 200));
    check(`失败文案给出后端自报的真实窗口（${BACKEND_WINDOW} tokens，而不是配置里的 32768）`,
      visible.includes(`${BACKEND_WINDOW} tokens`), visible.slice(0, 200));
    check('失败文案给出「新会话」指引', visible.includes('新会话'), visible.slice(0, 200));
    check('失败文案给出「设置 → 模型」指引', visible.includes('设置 → 模型'), visible.slice(0, 200));
    check('失败文案不再甩原始 JSON',
      !visible.includes('exceed_context_size_error') && !visible.includes('"code"'),
      visible.slice(0, 200));
    await page.screenshot({ path: shot('2_overflow_error') });

    // 后端真值：落库的就是这份可行动文案（刷新后照样能看到）。
    const failDetail = await apiJson(`/api/conversations/${FAIL_ID}`);
    const errorMessages = (failDetail.messages || []).filter((m) => m.role === 'error');
    check('后端落了一条 error 消息', errorMessages.length >= 1,
      JSON.stringify((failDetail.messages || []).map((m) => m.role)));
    const errorText = errorMessages.map((m) => String(m.content || '')).join('\n');
    check('落库文案含后端名与真实窗口',
      errorText.includes(PROVIDER_NAME) && errorText.includes(`${BACKEND_WINDOW} tokens`),
      errorText.slice(0, 200));
    check('落库文案含下一步动作', errorText.includes('新会话'), errorText.slice(0, 200));

    // ==================================================== ③ 收尾纪律
    const noise = pageErrors.filter((line) => !line.includes('favicon'));
    check('零 JS 错误', noise.length === 0, noise.slice(0, 3).join(' | '));
    const badHttp = httpErrors.filter((line) => !line.includes('favicon'));
    check('零 4xx/5xx 响应', badHttp.length === 0, badHttp.slice(0, 3).join(' | '));
  } catch (error) {
    check(`冒烟执行未抛异常（${error && error.message}）`, false,
      String((error && error.stack) || '').split('\n').slice(0, 6).join(' <- '));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED（${failures.length}）：${failures.join(' / ')}` : '\nALL CHECKS PASSED');
  process.exit(failures.length ? 1 : 0);
})();
