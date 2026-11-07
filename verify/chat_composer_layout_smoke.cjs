// 真实浏览器验证聊天输入区：HTML解析归属、长工具消息、自定义背景、编辑还原与三视图往返。
// 先起隔离源码服务，通过 NAIBA_SMOKE_BASE 指定地址；不读取或改写真实会话。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8765';
const VIEWPORTS = [{ width: 1580, height: 1033 }, { width: 1280, height: 800 }, { width: 375, height: 812 }];
const LONG_TEXT = '用于验证聊天布局的隔离内容，不执行任何命令。\n'.repeat(50);
const messages = [
  { id: 'layout-user', role: 'user', content: '隔离布局测试问题', metadata: {} },
  { id: 'layout-assistant', role: 'assistant', content: '布局测试回复末尾', metadata: {
    activity: Array.from({ length: 30 }, (_, index) => [
      { type: 'reasoning', text: `思考段 ${index}\n${LONG_TEXT}` },
      { type: 'tool', run: { tool: 'layout_probe', success: index % 3 !== 0, arguments: { index }, result: LONG_TEXT } },
    ]).flat(),
  } },
];

async function geometry(page, { editing = false } = {}) {
  return page.evaluate(({ editing }) => {
    const main = document.querySelector('.chat-main');
    const composer = document.querySelector('#composerForm').closest('.composer-wrap');
    const input = document.querySelector('#messageInput');
    const send = document.querySelector('#sendButton');
    const list = document.querySelector('#messages');
    const rect = element => {
      const r = element.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom };
    };
    const visible = element => {
      const r = rect(element), cs = getComputedStyle(element);
      return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'
        && r.x >= -1 && r.y >= -1 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1;
    };
    return {
      mainInShell: main.parentElement === document.querySelector('#appShell'),
      composerInMain: composer.parentElement === main,
      composerInEdit: Boolean(composer.closest('.message-row.user.is-editing')),
      inputVisible: visible(input), sendVisible: visible(send),
      input: rect(input), send: rect(send), composer: rect(composer), list: rect(list),
      listScrollHeight: list.scrollHeight, listClientHeight: list.clientHeight, listScrollTop: list.scrollTop,
      editingBody: document.body.classList.contains('is-editing-message'),
      launchBodyGroups: document.querySelectorAll('#superAgentLaunchDialog > .dlg-body > .launcher-group').length,
      launchFooter: Boolean(document.querySelector('#superAgentLaunchDialog > .dlg-foot')),
      horizontalOverflow: document.documentElement.scrollWidth > innerWidth,
    };
  }, { editing });
}

async function assertComposer(page, { editing = false, label = '' } = {}) {
  const state = await geometry(page, { editing });
  assert.equal(state.mainInShell, true, `${label} 主网格留在应用容器`);
  assert.equal(state.composerInMain, !editing, `${label} 输入区归属`);
  assert.equal(state.composerInEdit, editing, `${label} 编辑态归属`);
  assert.equal(state.editingBody, editing, `${label} 编辑态标记`);
  assert.equal(state.inputVisible, true, `${label} 输入框须完整位于视口内：${JSON.stringify(state)}`);
  assert.equal(state.sendVisible, true, `${label} 发送按钮须完整位于视口内：${JSON.stringify(state)}`);
  assert.equal(state.horizontalOverflow, false, `${label} 不产生横向页面溢出`);
  if (!editing) assert.ok(state.list.bottom <= state.composer.y + 1, `${label} 消息区不能覆盖输入区`);
  assert.equal(state.launchBodyGroups, 2, `${label} 两组启动卡均属于弹窗正文`);
  assert.equal(state.launchFooter, true, `${label} 启动台页脚仍属于弹窗`);
  return state;
}

async function rawHtmlCheck(browser, viewport) {
  const context = await browser.newContext({ viewport, javaScriptEnabled: false });
  const page = await context.newPage();
  try {
    await page.goto(BASE, { waitUntil: 'load' });
    await assertComposer(page, { label: '禁用JS后的原生HTML解析' });
  } finally { await context.close(); }
}

async function runViewport(browser, viewport) {
  await rawHtmlCheck(browser, viewport);
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(message.text());
  });
  page.on('response', response => {
    if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) errors.push(`HTTP ${response.status()} ${response.url()}`);
  });
  await page.addInitScript(() => { localStorage.clear(); localStorage.setItem('naibaOnboardingDismissed', '1'); });
  await page.route('**/api/providers/models', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"models":[]}' }));
  await page.route('**/api/file?**', route => route.fulfill({
    status: 200, contentType: 'image/svg+xml',
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000"><defs><linearGradient id="g"><stop stop-color="#7d92c7"/><stop offset="1" stop-color="#f3d8e9"/></linearGradient></defs><rect width="1600" height="1000" fill="url(#g)"/></svg>',
  }));
  const checkErrors = async () => {
    assert.deepEqual(errors, [], '页面不得出现脚本/接口错误');
    assert.equal(await page.locator('body').evaluate(el => /JS ERROR|PROMISE REJECT/.test(el.innerText)), false);
  };
  try {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await page.evaluate(async ({ messages }) => {
      const core = await import('/js/01-core.js');
      const chat = await import('/js/04-messages.js');
      // 恢复上次视图后，先同步控制器状态，不能只改 DOM 留下工作台打开标记。
      (await import('/js/22-studio.js')).closeStudioView();
      (await import('/js/28-super-agent.js')).closeSuperAgentView();
      core.setAppView('chat');
      core.state.conversationId = '';
      core.state.chatBusy = false;
      chat.renderMessages(messages);
      window.__layout = { core, chat, refs: await import('/js/13-skill-refs.js'), media: await import('/js/03-media.js') };
    }, { messages });
    await checkErrors();
    await assertComposer(page, { label: '长工具消息折叠态' });
    assert.equal(await page.locator('#messages details.tool-run').count(), 30);
    await page.evaluate(() => {
      document.querySelectorAll('#messages details').forEach(detail => { detail.open = true; });
      window.__layout.chat.forceScrollToBottom();
    });
    const expanded = await assertComposer(page, { label: '长工具消息全部展开' });
    assert.ok(expanded.listScrollHeight > expanded.listClientHeight * 5, '展开后的长消息必须由消息区独立滚动');
    assert.ok(expanded.listScrollHeight - expanded.listScrollTop - expanded.listClientHeight <= 2, '展开后仍可滚到最后一条回复');
    await checkErrors();

    await page.evaluate(() => {
      window.__layout.core.applyChatBackground({ image: 'uploads/layout-background.png', opacity: 0.8 });
      document.querySelectorAll('#messages details').forEach(detail => { detail.open = false; });
      window.__layout.chat.forceScrollToBottom();
    });
    assert.equal(await page.locator('html').getAttribute('data-chat-bg'), 'on');
    assert.notEqual(await page.locator('#chatBackdrop .chat-bg-surface').evaluate(el => getComputedStyle(el).backgroundImage), 'none');
    await assertComposer(page, { label: '启用自定义背景后' });
    await page.locator('#messageInput').fill('输入框在长工具对话后仍可输入');
    await page.locator('#messageInput').press('End');
    assert.equal(await page.locator('#messageInput').inputValue(), '输入框在长工具对话后仍可输入');
    await checkErrors();

    await page.evaluate(() => window.__layout.chat.startEditMessage(document.querySelector('[data-message-id="layout-user"]')));
    await page.locator('#messageInput').scrollIntoViewIfNeeded();
    await assertComposer(page, { editing: true, label: '编辑历史消息' });
    await page.locator('#messageInput').fill('编辑中的新文字');
    await page.evaluate(() => window.__layout.chat.cancelActiveEdit());
    // 按钮带120ms几何过渡，节点由气泡搬回底部后等待既有过渡结束再验收。
    await page.waitForTimeout(200);
    assert.equal(await page.locator('#messageInput').inputValue(), '输入框在长工具对话后仍可输入', '取消编辑还原底部草稿');
    await assertComposer(page, { label: '取消编辑还原' });
    await checkErrors();

    await page.evaluate(async () => { (await import('/js/22-studio.js')).openStudioView(); });
    assert.equal(await page.locator('#studioView').isVisible(), true);
    assert.equal(await page.locator('#messageInput').isVisible(), false);
    await page.evaluate(async () => { (await import('/js/22-studio.js')).closeStudioView(); });
    await assertComposer(page, { label: '工作台返回聊天' });
    await page.evaluate(async () => { await (await import('/js/28-super-agent.js')).openSuperAgentView(); });
    assert.equal(await page.locator('#superAgentView').isVisible(), true);
    await page.locator('#superAgentCreate').click();
    assert.equal(await page.locator('#superAgentLaunchDialog').isVisible(), true);
    assert.equal(await page.locator('#superAgentLaunchDialog [data-launch-domain]').count(), 5);
    await page.locator('#superAgentLaunchDialog [data-launch-close]').first().click();
    await page.evaluate(async () => { (await import('/js/28-super-agent.js')).closeSuperAgentView(); });
    await assertComposer(page, { label: '任务总管返回聊天' });
    await checkErrors();
    await page.evaluate(() => window.__layout.chat.forceScrollToBottom());
    await page.screenshot({ path: path.join(__dirname, `chat_composer_layout_${viewport.width}.png`) });
    console.log(`通过 ${viewport.width}×${viewport.height}：HTML归属、长工具折叠展开、背景、编辑还原、三视图与启动台`);
  } catch (error) {
    await page.screenshot({ path: path.join(__dirname, `chat_composer_layout_failed_${viewport.width}.png`) });
    throw error;
  } finally { await page.close(); }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try { for (const viewport of VIEWPORTS) await runViewport(browser, viewport); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
