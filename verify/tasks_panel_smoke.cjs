// 任务面板浏览器冒烟（Playwright + 系统 Edge）。
//
// 由 verify/tasks_panel_smoke.py 编排：起一个读仓库 public/ 的隔离源码实例、播种
// "回答记录 + 后台作业"混排的数据（含 chat → 子 Agent → ComfyUI 两层派生链），
// 然后在这里断言这次改造的六条承诺：
//   1. 面板只列后台作业（chat/plan_execute 那些"回答记录"不出现）；
//   2. 按回答分组，组标题 = 类型 + 时间，且不引用用户消息原文；
//   3. 状态一律中文（含「停止中」）；行内停止按钮只对活动作业出现；
//   4. 两层派生链同组、子行带嵌套标记（缩进 + `↳` 由真实 CSS 生效）；
//   5. 切会话只走显式「跳转」按钮（卡片本体不再是动作区，点空白不跳转）；
//      任务日志在列表整体重绘后不能被擦掉（cursor 只增，必须按缓存回填）；
//   6. 零 pageerror / 零 console.error；窄屏不横向滚动。
//
// 环境变量：NAIBA_SMOKE_BASE（默认 http://127.0.0.1:8811）
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8811';
const failures = [];

function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${!ok && detail ? `  -> ${detail}` : ''}`);
  if (!ok) failures.push(label);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const consoleErrors = [];
  const badResponses = [];
  page.on('pageerror', (error) => pageErrors.push(String((error && error.message) || error)));
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
  page.on('response', (response) => {
    if (response.status() >= 400) badResponses.push(`${response.status()} ${response.url()}`);
  });

  // 隔离实例里的假供应商 base_url 指向 example.invalid（故意的、不可达），打开会话时
  // 模型下拉会拉一次目录并必然 400。本冒烟不关心模型下拉，按既有做法把目录请求桩掉
  // （见 composer_model_smoke.cjs），这样 4xx/5xx 断言仍是零容忍。
  await page.route('**/api/providers/models', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ models: [] }),
  }));

  // 任务日志接口打桩：首次（after=0）给 5 行、cursor=5；此后如实返回「无新增」。
  // 用来钉死「列表整体重绘后，已看到的日志行不能被擦掉」——重绘出的空节点若只按
  // cursor 续拉，历史行会整块消失（这正是本次修掉的任务日志展示缺陷）。
  await page.route('**/api/jobs/**/events**', (route) => {
    const after = Number(new URL(route.request().url()).searchParams.get('after') || 0);
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(after === 0
        ? { events: [1, 2, 3, 4, 5].map((n) => ({ sequence: n, line: `冒烟日志第 ${n} 行` })), cursor: 5 }
        : { events: [], cursor: 5 }),
    });
  });

  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded' });
    // 面板渲染在轮询里完成（弹层没开也会重绘 #taskList）：等五条作业都到位再开弹层。
    await page.waitForFunction(
      () => document.querySelectorAll('#taskList .task-item').length === 5,
      null, { timeout: 30000 },
    );

    // 顶栏「任务」按钮：优先真点按钮（顺带验证接线），点不到再兜底直接开弹层。
    try {
      await page.click('#openTasks', { timeout: 5000 });
    } catch (error) {
      console.log(`NOTE  顶栏「任务」按钮点击失败（${error.message.split('\n')[0]}），改为直接开弹层`);
      await page.evaluate(() => document.querySelector('#tasksDialog').showModal());
    }
    await page.waitForSelector('#tasksDialog[open] .task-item', { timeout: 10000 });

    const itemIds = await page.$$eval('#tasksDialog .task-item', (nodes) => nodes.map((n) => n.dataset.taskId));
    check('面板只列后台作业（5 条）', itemIds.length === 5, JSON.stringify(itemIds));
    check('回答记录不出现在面板',
      !itemIds.includes('smoke-reply-1') && !itemIds.includes('smoke-reply-2'), JSON.stringify(itemIds));

    const titles = await page.$$eval('#tasksDialog .task-group-title', (nodes) => nodes.map((n) => n.textContent.trim()));
    check('组标题 = 类型 + 时间', titles.length === 2 && titles.every((t) => t.includes('·')) && titles.every((t) => t.includes('ComfyUI 生成')),
      JSON.stringify(titles));
    check('同批两个作业显示 ×2', titles.some((t) => t.includes('×2')), JSON.stringify(titles));
    check('组标题不引用用户消息原文', !titles.some((t) => t.includes('不该出现在面板里')), JSON.stringify(titles));
    check('组标题把 subagent 标为「AI 子任务」', titles.some((t) => t.includes('AI 子任务')), JSON.stringify(titles));

    // 行标题优先显示落库任务名（message = JobSpec.label），类型名退居 hover 提示。
    const kindTitles = await page.$$eval('#tasksDialog .task-item', (nodes) => nodes.map((n) => ({
      id: n.dataset.taskId,
      title: (n.querySelector('.task-kind')?.textContent || '').trim(),
      tooltip: (n.querySelector('.task-kind')?.getAttribute('title') || '').trim(),
    })));
    const subagentRow = kindTitles.find((row) => row.id === 'smoke-job-subagent') || { title: '', tooltip: '' };
    check('子任务行标题显示任务名而非类型名',
      subagentRow.title === '子任务：整理素材并提交出图' && subagentRow.title !== 'AI 子任务',
      JSON.stringify(subagentRow));
    check('行标题 hover 提示保底显示类型名', subagentRow.tooltip === 'AI 子任务', JSON.stringify(subagentRow));

    const statuses = await page.$$eval('#tasksDialog .task-item .task-status', (nodes) => nodes.map((n) => n.textContent.trim()));
    check('状态显示为「停止中」', statuses.includes('停止中'), JSON.stringify(statuses));
    check('状态全为中文（没有漏出英文状态码）', statuses.every((s) => /[\u4e00-\u9fa5]/.test(s)), JSON.stringify(statuses));

    const cancelIds = await page.$$eval('#tasksDialog [data-task-cancel]', (nodes) => nodes.map((n) => n.dataset.taskCancel));
    check('运行中的作业有停止按钮', cancelIds.includes('smoke-job-running'), JSON.stringify(cancelIds));
    check('停止中的作业仍保留停止按钮', cancelIds.includes('smoke-job-stopping'), JSON.stringify(cancelIds));
    check('终态作业没有停止按钮', !cancelIds.includes('smoke-job-failed'), JSON.stringify(cancelIds));

    const summary = (await page.textContent('#taskSummary')) || '';
    // 「运行中」= 活动态总数（含 stopping / queued / waiting）：本批 3 running + 1 stopping。
    check('统计条口径', /共 5 · 运行中 4 · 失败 1 · 已完成 0/.test(summary), summary);

    // 两层派生链（chat → 子 Agent → ComfyUI）：chat 行被 jobs_only 过滤后，子 Agent 与它
    // 派生的 ComfyUI 仍得在同一组里；子行还要有嵌套标记，且缩进/`↳` 必须由真实 CSS 生效
    // （只断言类名会把"样式没打进去"这种事故放过去）。
    const sections = await page.$$eval('#tasksDialog .task-group', (nodes) => nodes.map((section) => ({
      title: (section.querySelector('.task-group-title')?.textContent || '').trim(),
      ids: [...section.querySelectorAll('.task-item')].map((n) => n.dataset.taskId),
      nested: [...section.querySelectorAll('.task-item')].map((n) => n.classList.contains('task-item-nested')),
    })));
    check('派生链同组（作业不按单层父 id 裂开）', sections.length === 2, JSON.stringify(sections));
    const chain = sections.find((section) => section.ids.includes('smoke-job-subagent')) || { ids: [], nested: [] };
    check('子 Agent 与它派生的 ComfyUI 在同一组',
      chain.ids.length === 3 && chain.ids.includes('smoke-job-nested') && chain.ids.includes('smoke-job-failed'),
      JSON.stringify(chain.ids));
    // 缩进口径跟「父行是否也在面板里」走：失败作业的父（chat 行）被过滤 → 不缩进；
    // 子 Agent 的父就是那条失败作业 → 缩进；它派生的 ComfyUI 同理。
    const nestedById = new Map(chain.ids.map((id, index) => [id, chain.nested[index]]));
    check('只有父行可见的作业才缩进（父行被过滤的那个不缩进）',
      nestedById.get('smoke-job-failed') === false
      && nestedById.get('smoke-job-subagent') === true
      && nestedById.get('smoke-job-nested') === true,
      JSON.stringify([...nestedById]));

    const nestedStyle = await page.$eval('#tasksDialog .task-item-nested', (node) => ({
      marginLeft: getComputedStyle(node).marginLeft,
      indentMark: getComputedStyle(node.querySelector('.task-kind'), '::before').content,
    }));
    check('嵌套缩进与 ↳ 标记由真实 CSS 生效',
      parseFloat(nestedStyle.marginLeft) > 0 && String(nestedStyle.indentMark).includes('↳'),
      JSON.stringify(nestedStyle));

    const detailButton = await page.$('#tasksDialog [data-task-detail="smoke-job-failed"]');
    check('失败作业有详情入口', Boolean(detailButton));
    if (detailButton) {
      await detailButton.click();
      const detailText = (await page.textContent('#tasksDialog [data-task-id="smoke-job-failed"] .task-detail')) || '';
      const visible = await page.isVisible('#tasksDialog [data-task-id="smoke-job-failed"] .task-detail');
      check('详情可展开且含失败原因', visible && detailText.includes('第 1 段提交失败'), detailText.slice(0, 120));
    }

    // 跳转：卡片本体不再是动作区（旧行为点任意空白即切会话并关面板），切会话只走显式按钮。
    const openIds = await page.$$eval('#tasksDialog [data-task-open]', (nodes) => nodes.map((n) => n.dataset.taskOpen));
    check('每行都有「跳转」按钮', openIds.length === 5 && openIds.includes('smoke-job-running'), JSON.stringify(openIds));
    await page.click('#tasksDialog [data-task-id="smoke-job-failed"] .task-kind');
    await page.waitForTimeout(250);
    const stillOpen = await page.evaluate(() => Boolean(document.querySelector('#tasksDialog[open]')));
    check('点任务名/空白不再跳转（面板保持打开）', stillOpen);

    // 任务日志：展开详情会拉起 5 行（上面的桩按 cursor 给增量）；列表每轮轮询都会
    // 整体重建 DOM，重绘后这些行必须还在（修复前只剩 cursor 之后的新行 → 整块消失）。
    const logLineSel = '#tasksDialog [data-task-id="smoke-job-failed"] .task-log-line';
    await page.waitForFunction(
      (sel) => document.querySelectorAll(sel).length > 0, logLineSel, { timeout: 8000 },
    ).catch(() => {});
    const logLinesBefore = await page.$$eval(logLineSel, (nodes) => nodes.length);
    check('展开后任务日志按 cursor 拉到 5 行', logLinesBefore === 5, `${logLinesBefore}`);

    // 给当前行节点打个标记：轮询重绘会换掉节点，标记消失即证明「确实重绘过一轮」。
    const itemSel = '#tasksDialog [data-task-id="smoke-job-failed"]';
    await page.evaluate((sel) => {
      const item = document.querySelector(sel);
      if (item) item.dataset.logMark = '1';
    }, itemSel);
    const repainted = await page.waitForFunction(
      (sel) => document.querySelector(sel)?.dataset.logMark !== '1', itemSel, { timeout: 10000 },
    ).then(() => true).catch(() => false);
    check('等到了列表整体重绘（轮询重建 DOM）', repainted);
    // 重绘后的回填是异步的：等日志回到 5 行（修复前会一直停在 0，这份断言必红）。
    const logKept = await page.waitForFunction(
      (sel) => document.querySelectorAll(sel).length >= 5, logLineSel, { timeout: 8000 },
    ).then(() => true).catch(() => false);
    const logLinesAfter = await page.$$eval(logLineSel, (nodes) => nodes.length);
    check('重绘后已看到的日志行没有被擦掉', logKept && logLinesAfter >= 5, `${logLinesAfter}`);

    // Playwright 截图留档：验证前端显示效果的人工核对证据（verify/ 在 .gitignore）。
    const fs = require('fs');
    const path = require('path');
    const shotsDir = path.join(__dirname, 'tasks_panel_shots');
    fs.mkdirSync(shotsDir, { recursive: true });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: path.join(shotsDir, 'desktop-panel.png') });
    await page.setViewportSize({ width: 430, height: 900 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check('430px 视口无横向滚动', overflow <= 0, `overflow=${overflow}`);
    await page.screenshot({ path: path.join(shotsDir, 'mobile-panel.png') });

    check('零 pageerror', pageErrors.length === 0, pageErrors.join(' | '));
    check('零 console.error', consoleErrors.length === 0, consoleErrors.join(' | '));
    check('无 4xx/5xx 响应', badResponses.length === 0, badResponses.join(' | '));
  } catch (error) {
    check(`冒烟执行异常：${error.message.split('\n')[0]}`, false);
  } finally {
    await browser.close();
  }

  console.log(`\n结果：${failures.length ? `失败 ${failures.length} 项：${failures.join(' / ')}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
