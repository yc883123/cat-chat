// Agent 说明弹层 + 工作区路径 / 未注册分组提示 的浏览器侧检查。
// 由 verify/agent_help_popover.py 自编排：它先起一个**隔离的真实源码实例**（独立 data_dir /
// config / 端口），再把地址通过 NAIBA_SMOKE_BASE 传进来。本文件只管浏览器里的事。
//
// 为什么不再用「静态 public/ 服务」（2026-09-17 改）：断言确实不需要后端，但**截图需要**——
// 静态服务下 `/api/starter-prompts` 404，开始页只剩 index.html 写死的那两张 Skill 卡，
// 配置驱动的 6 张全没了，截图看起来像"卡片少了一大半"，而断言照样全绿（§九.92）。
//
// 环境变量：NAIBA_SMOKE_BASE（必填）、NAIBA_EXPECT_STARTERS（内置预设张数，用于断言卡片渲染齐）
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || '';
const EXPECT_STARTERS = Number(process.env.NAIBA_EXPECT_STARTERS || '0');
const ROOT = path.resolve(__dirname, '..');

const failures = [];
function fail(label, detail = '') {
  console.log(`FAIL  ${label}${detail ? ` -> ${detail}` : ''}`);
  failures.push(label);
}
function pass(label) { console.log(`PASS  ${label}`); }

(async () => {
  if (!BASE) {
    fail('缺少 NAIBA_SMOKE_BASE', '请通过 verify/agent_help_popover.py 运行');
    process.exit(1);
  }
  let code = 1;
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const pageErrors = [];
    const badResponses = [];
    page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
    page.on('console', (msg) => {
      // 「Failed to load resource」不带 URL，无法和已知噪音区分；接口层面的 4xx/5xx 由下面的
      // badResponses 逐条带 URL 断言，所以这里只收真正的 console.error 文案。
      if (msg.type() === 'error' && !msg.text().includes('Failed to load resource')) {
        pageErrors.push(`console.error: ${msg.text()}`);
      }
    });
    // 已知取样噪音：隔离实例的供应商是 `https://example.invalid/v1`（_serve_tmp.py 的占位），
    // 开场页的「检测模型目录」必然连不上 → 后端回 400。这是 harness 的属性，不是页面问题；
    // 其余任何 4xx/5xx 都照旧算失败。
    const KNOWN_NOISE = /\/api\/providers\/models$/;
    page.on('response', (res) => {
      if (res.status() >= 400 && !KNOWN_NOISE.test(res.url())) {
        badResponses.push(`${res.status()} ${res.url()}`);
      }
    });
    await page.goto(`${BASE}/index.html`, { waitUntil: 'load' });
    await page.waitForTimeout(1500);
    // 关掉「会话同步」轮询（10s 一跳，会按服务端真实状态重渲染 composer 下拉）：
    // 后面几组断言要往 state 里注入工作区/会话，注入必须活到截图那一刻——
    // 否则就是 §九.90 那个错法（拍到的不是被断言的状态）。只动这一个轮询，其余照常。
    await page.evaluate(async () => {
      const core = await import('./js/01-core.js');
      core.state.syncPolling = false;
      if (core.state.syncTimer) { window.clearTimeout(core.state.syncTimer); core.state.syncTimer = null; }
    });

    // ① 开始页形态：空态可见 + 卡片渲染齐（静态服务就是在这里悄悄丢卡的）
    const empty = await page.evaluate(() => {
      const es = document.querySelector('#emptyState');
      const grid = document.querySelector('.starter-grid');
      return {
        visible: Boolean(es) && !es.hidden && es.offsetHeight > 0,
        total: grid ? grid.children.length : 0,
        custom: grid ? grid.querySelectorAll('.custom-starter').length : 0,
        titles: grid ? [...grid.children].map((c) => c.querySelector('.starter-title')?.textContent || '') : [],
      };
    });
    if (empty.visible) pass('开始页空态可见');
    else fail('开始页空态不可见（页面不是干净的新会话形态）');
    if (EXPECT_STARTERS > 0 && empty.custom === EXPECT_STARTERS) {
      pass(`配置驱动的快捷卡片渲染齐（${empty.custom}/${EXPECT_STARTERS}）`);
    } else {
      fail('配置驱动的快捷卡片数量不对', `custom=${empty.custom} 期望=${EXPECT_STARTERS}`);
    }
    if (empty.total === EXPECT_STARTERS + 3) {
      pass(`开始页卡片总数正确（${empty.total} = 预设 ${EXPECT_STARTERS} + Skill 2 + 自定义指令 1）`);
    } else {
      fail('开始页卡片总数不对', `total=${empty.total} 期望=${EXPECT_STARTERS + 3} titles=${JSON.stringify(empty.titles)}`);
    }

    // ② 「?」按钮 + 说明层
    const helpBtn = page.locator('#agentHelpButton');
    if (await helpBtn.isVisible()) pass('Agent 说明按钮可见');
    else fail('Agent 说明按钮不可见');
    await page.screenshot({ path: path.join(ROOT, 'verify', 'agent_help_button.png'), clip: { x: 0, y: 0, width: 1280, height: 64 } });

    await helpBtn.click();
    await page.waitForTimeout(200);
    const popoverInfo = await page.evaluate(() => {
      const el = document.querySelector('#agentHelpPopover');
      if (!el || el.hidden) return { open: false };
      const text = el.textContent || '';
      return {
        open: true,
        inBody: el.parentElement === document.body,
        hasHttp: text.includes('HTTP 直连'),
        hasMcp: text.includes('MCP 连接'),
        hasPreset: text.includes('ComfyUI 联动'),
        hasAgents: text.includes('通用 Agent') && text.includes('编程 Agent') && text.includes('短剧 Agent'),
        expanded: document.querySelector('#agentHelpButton')?.getAttribute('aria-expanded'),
      };
    });
    if (popoverInfo.open) pass('点击后说明层打开');
    else fail('点击后说明层未打开');
    if (popoverInfo.inBody) pass('说明层已挂到 body（不被顶栏裁剪）');
    else fail('说明层未挂到 body');
    if (popoverInfo.hasHttp && popoverInfo.hasMcp) pass('说明含 ComfyUI HTTP / MCP 两条路线');
    else fail('说明缺少 ComfyUI 两条路线', JSON.stringify(popoverInfo));
    if (popoverInfo.hasPreset) pass('说明含「ComfyUI 联动」工具预设指引');
    else fail('说明缺少工具预设指引');
    if (popoverInfo.hasAgents) pass('说明含三种预设 Agent');
    else fail('说明缺少预设 Agent 介绍');
    if (popoverInfo.expanded === 'true') pass('aria-expanded 同步');
    else fail('aria-expanded 未同步', String(popoverInfo.expanded));
    await page.screenshot({ path: path.join(ROOT, 'verify', 'agent_help_popover.png') });

    // ③ 点外部关闭（落点取右下空白，别踩到开始页卡片：点卡片会往输入框塞文字）
    await page.mouse.click(1150, 700);
    await page.waitForTimeout(150);
    const closed = await page.evaluate(() => document.querySelector('#agentHelpPopover')?.hidden === true);
    if (closed) pass('点外部后说明层关闭');
    else fail('点外部后说明层未关闭');

    // ④ 工作区路径悬停提示（注入两条同名不同目录的工作区，再调真实渲染函数）
    const wsResult = await page.evaluate(async () => {
      const core = await import('./js/01-core.js');
      const conv = await import('./js/08-conversations.js');
      core.state.workspaces = [
        { name: '素材4', dir: 'D:\\海螺H3提示词工程\\素材4' },
        { name: '素材4', dir: 'E:\\backup\\素材4' },
      ];
      core.state.conversations = [{ id: 'c1', workspace_group: '素材4' }];
      core.state.conversationId = 'c1';
      conv.renderComposerWorkspace();
      const sel = document.querySelector('#composerWorkspaceSelect');
      const options = [...sel.options].map((o) => ({ value: o.value, title: o.title }));
      const headerHtml = conv.sidebarRowHtml({ type: 'header', wsName: '素材4', label: '素材4', dir: 'D:\\海螺H3提示词工程\\素材4', isExp: false, count: 2 });
      const ungroupedHtml = conv.sidebarRowHtml({ type: 'header', wsName: '', label: '未分组', dir: '', isUngrouped: true, isExp: false, count: 1 });
      return { options, selectTitle: sel.title, headerHtml, ungroupedHtml };
    });
    const dirTip = wsResult.options.find((o) => o.value === '素材4');
    if (dirTip && dirTip.title.includes('素材4')) pass('下拉 option.title 带目录路径');
    else fail('下拉 option.title 缺目录路径', JSON.stringify(wsResult.options));
    if (wsResult.options[0] && wsResult.options[0].title) pass('「未分组」option 也有提示');
    else fail('「未分组」option 缺提示');
    if (wsResult.selectTitle.includes('D:')) pass('select.title 显示当前工作区目录');
    else fail('select.title 未显示当前目录', wsResult.selectTitle);
    if (wsResult.headerHtml.includes('title="素材4（D:\\海螺H3提示词工程\\素材4）"')) pass('侧栏分组头 title = 名字（目录）');
    else fail('侧栏分组头 title 不对', wsResult.headerHtml);
    if (wsResult.ungroupedHtml.includes('title="未分组"')) pass('未分组分组头 title 兜底为名字');
    else fail('未分组分组头 title 不对', wsResult.ungroupedHtml);

    // ⑤ 未注册分组：注册表为空（源码模式实测形态）+ 会话属于 naiba-chat。
    //    注意截图必须趁「未注册」这个状态还在——把"补齐注册表"的回正步骤写在同一个 evaluate 里，
    //    截图时状态早就被覆盖了（§九.90）。故拆成两步。
    const orphanResult = await page.evaluate(async () => {
      const core = await import('./js/01-core.js');
      const conv = await import('./js/08-conversations.js');
      core.state.conversations = [{ id: 'c1', workspace_group: 'naiba-chat' }];
      core.state.conversationId = 'c1';
      core.state.workspaces = [];
      conv.renderComposerWorkspace();
      const sel = document.querySelector('#composerWorkspaceSelect');
      return {
        values: [...sel.options].map((o) => o.value),
        labels: [...sel.options].map((o) => o.textContent),
        value: sel.value,
        title: sel.title,
      };
    });
    const orphanOption = orphanResult.values.filter((v) => v === 'naiba-chat').length;
    if (orphanOption === 1) pass('注册表为空时下拉仍保住会话所属分组那一项');
    else fail('未注册分组没进下拉', JSON.stringify(orphanResult));
    if (orphanResult.value === 'naiba-chat') pass('未注册分组的会话不再退化成「未分组」');
    else fail('未注册分组仍退化成未分组', `value=${orphanResult.value}`);
    const orphanLabel = orphanResult.labels.find((t) => t.includes('naiba-chat')) || '';
    if (orphanLabel.includes('未注册')) pass('未注册项在标签上标出「未注册」');
    else fail('未注册项没有标记', orphanLabel);
    if (orphanResult.title.includes('未在本实例注册') && orphanResult.title.includes('新建工作区')) {
      pass('select.title 说明未注册并给出恢复路径');
    } else {
      fail('select.title 未说明未注册与恢复路径', orphanResult.title);
    }
    // 截图落在下拉本身（原生闭合 select 会显示选中项文字，正是「naiba-chat（未注册）」）。
    const wsSelect = page.locator('#composerWorkspaceSelect');
    await wsSelect.scrollIntoViewIfNeeded();
    await page.waitForTimeout(120);
    await wsSelect.screenshot({ path: path.join(ROOT, 'verify', 'agent_help_orphan_workspace.png') });

    // 补齐注册表：同一会话回到正常形态
    const restoredResult = await page.evaluate(async () => {
      const core = await import('./js/01-core.js');
      const conv = await import('./js/08-conversations.js');
      core.state.workspaces = [{ name: 'naiba-chat', dir: 'D:\\repos\\naiba-chat' }];
      conv.renderComposerWorkspace();
      const sel = document.querySelector('#composerWorkspaceSelect');
      return {
        values: [...sel.options].map((o) => o.value),
        labels: [...sel.options].map((o) => o.textContent),
        value: sel.value,
        title: sel.title,
      };
    });
    if (restoredResult.value === 'naiba-chat'
      && restoredResult.labels.filter((t) => t.includes('naiba-chat'))[0]
      && !restoredResult.labels.some((t) => t.includes('未注册'))) {
      pass('注册表补齐后同一会话回到正常形态（无「未注册」标记）');
    } else {
      fail('注册表补齐后形态不对', JSON.stringify(restoredResult));
    }

    if (pageErrors.length) fail('页面无 JS 错误', pageErrors.join(' | '));
    else pass('页面无 JS 错误');
    if (badResponses.length) fail('无失败的接口请求', badResponses.join(' | '));
    else pass('无失败的接口请求');

    code = failures.length ? 1 : 0;
  } catch (error) {
    fail('运行异常', String((error && error.message) || error));
  } finally {
    await browser.close();
  }
  console.log(failures.length ? `\n${failures.length} 项失败` : '\n全部通过');
  process.exit(code);
})();
