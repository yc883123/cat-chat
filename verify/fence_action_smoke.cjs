/**
 * 围栏来源确认卡的前端验收（2026-10-01，配合 verify/fence_action_smoke.py 的 B 段）。
 *
 * 要验的是**用户真能看到、真能点**的那一层：
 *  - 整条响应是一段围栏动作时，确认卡必须出现，且按钮是 fenced 专属形态
 *    （文案「允许本轮继续执行后续操作」+ `data-allow-run="1"` + 一行说明）；
 *  - 点它之后**同一个 Run 的第二个围栏动作不再弹卡**（全程只有 1 张卡）；
 *  - 两个动作都真的执行了、最终答复到位；
 *  - 手机宽度（375px）下卡不溢出、不产生横向滚动。
 *
 * 环境变量：NAIBA_SMOKE_BASE / NAIBA_FENCE_S2_TITLE / NAIBA_FENCE_S2M_TITLE
 */
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8822';
const TITLE_DESKTOP = process.env.NAIBA_FENCE_S2_TITLE || 'S2 整条围栏授权';
const TITLE_MOBILE = process.env.NAIBA_FENCE_S2M_TITLE || 'S2M 手机围栏授权';
const FINAL_ANSWER = '两步都做完了。';

let failures = 0;
function check(condition, label, extra = '') {
  if (condition) {
    console.log(`  ✓ ${label}`);
  } else {
    failures += 1;
    console.log(`  ✗ ${label}${extra ? ` —— ${extra}` : ''}`);
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 记录发出去的聊天/事件请求——失败现场靠它区分「没发出去」和「发出去了但没反应」。 */
async function trackRequests(page) {
  await page.addInitScript(() => {
    window.__fenceRequests = [];
    const original = window.fetch;
    window.fetch = function patched(...args) {
      try {
        const raw = args[0];
        const url = String(raw && raw.url ? raw.url : raw);
        if (url.includes('/api/chat') || url.includes('/api/runs/')) {
          window.__fenceRequests.push(url);
        }
      } catch (err) {
        /* 记录失败不影响正常请求 */
      }
      return original.apply(this, args);
    };
  });
}

async function waitFor(fn, { timeout = 30000, interval = 200, label = '条件' } = {}) {
  const end = Date.now() + timeout;
  let last = null;
  while (Date.now() < end) {
    try {
      last = await fn();
      if (last) return last;
    } catch (err) {
      last = err.message;
    }
    await sleep(interval);
  }
  throw new Error(`等待超时：${label}（最后结果 ${JSON.stringify(last)}）`);
}

/** 打开指定标题的会话并把模型选好（不选发送键恒灰）。 */
async function openConversationByTitle(page, title) {
  await waitFor(
    () => page.evaluate(async (wanted) => {
      const core = await import('/js/01-core.js');
      const raw = core?.state?.conversations;
      const list = Array.isArray(raw) ? raw : Object.values(raw || {});
      return list.some((c) => c && c.title === wanted);
    }, title),
    { label: `侧栏出现会话「${title}」`, timeout: 30000 },
  );
  const conversationId = await page.evaluate(async (wanted) => {
    const core = await import('/js/01-core.js');
    const conv = await import('/js/08-conversations.js');
    const raw = core?.state?.conversations;
    const list = Array.isArray(raw) ? raw : Object.values(raw || {});
    const row = list.find((c) => c && c.title === wanted);
    if (!row) return '';
    await conv.openConversation(row.id);
    return row.id;
  }, title);
  if (!conversationId) throw new Error(`侧栏里找不到会话「${title}」`);
  await sleep(800);
  // 发送键在空输入框时本来就是灰的，不能拿它当"模型选好了没"的判据——
  // 真正的判据是输入区的模型下拉有没有值。没有就补一次模型目录 + 选中。
  const modelReady = await page.evaluate(async () => {
    const models = await import('/js/07-models-agents.js');
    let select = document.querySelector('#composerModelSelect');
    if (!select || !select.value) {
      await models.populateComposerModels();
      select = document.querySelector('#composerModelSelect');
    }
    if (select && !select.value) {
      const option = Array.from(select.options).find((item) => item.value);
      if (option) {
        select.value = option.value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
        await models.saveComposerModelSelection();
      }
    }
    select = document.querySelector('#composerModelSelect');
    return Boolean(select && select.value);
  });
  check(modelReady, `「${title}」输入区已绑定模型（否则发送键恒灰）`);
  await sleep(400);
  return conversationId;
}

/** 发出用户消息（内容 = 会话标题，假模型据此选剧本）并等确认卡。 */
async function sendAndAwaitConfirm(page, message) {
  const outcome = await page.evaluate(async (text) => {
    const stream = await import('/js/11-run-stream.js');
    try {
      stream.sendChatMessage(text);
      return { ok: true };
    } catch (err) {
      return { ok: false, error: String((err && err.message) || err) };
    }
  }, message);
  check(outcome.ok, '发送动作不抛异常', outcome.error || '');
  await waitFor(
    () => page.evaluate(() => document.querySelectorAll('.tool-confirm').length > 0),
    { label: '确认卡出现', timeout: 60000 },
  ).catch(async (err) => {
    // 失败时把现场说清楚：请求发出去没有、界面上写的是什么。
    const snapshot = await page.evaluate(async () => {
      const core = await import('/js/01-core.js');
      return {
        chatRequests: window.__fenceRequests || [],
        status: document.querySelector('#runtimeStatus')?.textContent || '',
        toast: document.querySelector('#toast')?.textContent || '',
        runId: core.state.chatRunId || '',
        conversationId: core.state.conversationId || '',
        messages: (document.querySelector('#messages')?.innerText || '').slice(0, 400),
      };
    });
    throw new Error(`${err.message}；现场=${JSON.stringify(snapshot)}`);
  });
}

async function assertCardShape(page, label) {
  const shape = await page.evaluate(() => {
    const card = document.querySelector('.tool-confirm');
    if (!card) return null;
    const approve = card.querySelector('.tool-confirm-approve');
    const reject = card.querySelector('.tool-confirm-reject');
    return {
      text: (approve?.textContent || '').trim(),
      allowRun: approve?.getAttribute('data-allow-run'),
      hasHint: Boolean(card.querySelector('.tool-confirm-hint')),
      hintText: (card.querySelector('.tool-confirm-hint')?.textContent || '').trim(),
      hasReject: Boolean(reject),
      cards: document.querySelectorAll('.tool-confirm').length,
    };
  });
  check(Boolean(shape), `${label} 确认卡必须渲染`);
  if (!shape) return;
  check(shape.text === '允许本轮继续执行后续操作', `${label} 按钮文案是 fenced 专属形态`, shape.text);
  check(shape.allowRun === '1', `${label} 按钮带 data-allow-run=1`, String(shape.allowRun));
  check(shape.hasHint && shape.hintText.includes('代码块'), `${label} 有「来自代码块」的说明行`);
  check(shape.hasReject, `${label} 保留「拒绝」按钮`);
  check(shape.cards === 1, `${label} 此刻只应有 1 张确认卡`, String(shape.cards));
}

async function approveRun(page) {
  await page.evaluate(async () => {
    const input = await import('/js/12-chat-input.js');
    const card = document.querySelector('.tool-confirm');
    const approve = card.querySelector('.tool-confirm-approve');
    await input.approveTool(
      approve.getAttribute('data-confirm-id'),
      approve.getAttribute('data-run-id'),
      true,
    );
  });
}

async function assertFinished(page, label) {
  const state = await waitFor(
    () => page.evaluate(() => {
      const text = document.querySelector('#messages')?.innerText || '';
      return text.includes('两步都做完了。') ? { text } : null;
    }),
    { label: `${label} 最终答复出现`, timeout: 90000 },
  );
  const info = await page.evaluate(() => ({
    cards: document.querySelectorAll('.tool-confirm').length,
    tools: Array.from(document.querySelectorAll('.tool-run-title, .tool-run')).map(
      (el) => el.textContent || '',
    ),
  }));
  check(info.cards === 1, `${label} 全程只弹了 1 张确认卡`, `实际 ${info.cards} 张`);
  const joined = info.tools.join(' | ');
  check(joined.includes('list_directory'), `${label} 第一个围栏动作执行了`);
  check(joined.includes('read_file'), `${label} 第二个围栏动作免确认直接执行了`);
  return state;
}

async function mobileGeometry(page, label) {
  const geometry = await page.evaluate(() => {
    const card = document.querySelector('.tool-confirm');
    if (!card) return null;
    const box = card.getBoundingClientRect();
    const buttons = Array.from(card.querySelectorAll('.tool-confirm-btn')).map((b) => {
      const rect = b.getBoundingClientRect();
      return { left: rect.left, right: rect.right, width: rect.width, height: rect.height };
    });
    return {
      box: { left: box.left, right: box.right, width: box.width },
      buttons,
      docScrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    };
  });
  check(Boolean(geometry), `${label} 确认卡存在`);
  if (!geometry) return;
  const { box, buttons } = geometry;
  check(box.left >= -0.5 && box.right <= geometry.innerWidth + 0.5,
    `${label} 卡完整落在视口内`, JSON.stringify(box));
  buttons.forEach((btn, index) => {
    check(btn.left >= box.left - 0.5 && btn.right <= box.right + 0.5,
      `${label} 第 ${index + 1} 个按钮未溢出卡外`, JSON.stringify(btn));
    check(btn.height >= 32, `${label} 第 ${index + 1} 个按钮可点（高度 ${Math.round(btn.height)}）`);
  });
  check(geometry.docScrollWidth <= geometry.innerWidth + 1,
    `${label} 页面无横向滚动`, `${geometry.docScrollWidth} > ${geometry.innerWidth}`);
}

async function desktopFlow(browser) {
  console.log('【桌面 1440×900】');
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(String(err)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console: ${msg.text()}`);
  });
  await trackRequests(page);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => Boolean(document.querySelector('#sendButton'))),
    { label: '应用启动', timeout: 30000 });
  await sleep(1200);
  await openConversationByTitle(page, TITLE_DESKTOP);
  await assertCardShapeFor(page, '桌面');
  await sendAndAwaitConfirm(page, TITLE_DESKTOP);
  await assertCardShape(page, '桌面');
  await approveRun(page);
  await assertFinished(page, '桌面');
  check(errors.length === 0, '桌面零 pageerror', errors.join(' | '));
  await context.close();
}

/** 打开会话后先断言卡不存在（说明动作还没发），避免把"一直没卡"当成通过。 */
async function assertCardShapeFor(page, label) {
  const count = await page.evaluate(() => document.querySelectorAll('.tool-confirm').length);
  check(count === 0, `${label} 发送前不应有确认卡`);
}

async function mobileFlow(browser) {
  console.log('【手机 375×720】');
  const context = await browser.newContext({
    viewport: { width: 375, height: 720 },
    deviceScaleFactor: 3,
    hasTouch: true,
    isMobile: true,
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(String(err)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console: ${msg.text()}`);
  });
  await trackRequests(page);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await sleep(1500);
  await openConversationByTitle(page, TITLE_MOBILE);
  await sendAndAwaitConfirm(page, TITLE_MOBILE);
  await assertCardShape(page, '手机');
  await mobileGeometry(page, '手机');
  await approveRun(page);
  await assertFinished(page, '手机');
  check(errors.length === 0, '手机零 pageerror', errors.join(' | '));
  await context.close();
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    await desktopFlow(browser);
    await mobileFlow(browser);
  } catch (err) {
    failures += 1;
    console.log(`  ✗ 浏览器段异常：${err.message}`);
  } finally {
    await browser.close();
  }
  if (failures) {
    console.log(`\n浏览器段失败（${failures} 项）`);
    process.exit(1);
  }
  console.log('\n浏览器段通过：确认卡形态、Run 级一次授权、桌面与手机版式全部符合预期。');
  process.exit(0);
})();
