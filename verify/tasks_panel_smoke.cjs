// 任务面板浏览器冒烟（Playwright + 系统 Edge）。
//
// 由 verify/tasks_panel_smoke.py 编排：起一个读仓库 public/ 的隔离源码实例、播种
// "回答记录 + 后台作业"混排的数据，然后在这里断言这次改造的四条承诺：
//   1. 面板只列后台作业（chat/plan_execute 那些"回答记录"不出现）；
//   2. 按回答分组，组标题 = 类型 + 时间，且不引用用户消息原文；
//   3. 状态一律中文（含「停止中」）；行内停止按钮只对活动作业出现；
//   4. 零 pageerror / 零 console.error；窄屏不横向滚动。
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

  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded' });
    // 面板渲染在轮询里完成（弹层没开也会重绘 #taskList）：等三条作业都到位再开弹层。
    await page.waitForFunction(
      () => document.querySelectorAll('#taskList .task-item').length === 3,
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
    check('面板只列后台作业（3 条）', itemIds.length === 3, JSON.stringify(itemIds));
    check('回答记录不出现在面板',
      !itemIds.includes('smoke-reply-1') && !itemIds.includes('smoke-reply-2'), JSON.stringify(itemIds));

    const titles = await page.$$eval('#tasksDialog .task-group-title', (nodes) => nodes.map((n) => n.textContent.trim()));
    check('组标题 = 类型 + 时间', titles.length === 2 && titles.every((t) => t.includes('·')) && titles.every((t) => t.includes('ComfyUI 生成')),
      JSON.stringify(titles));
    check('同批两个作业显示 ×2', titles.some((t) => t.includes('×2')), JSON.stringify(titles));
    check('组标题不引用用户消息原文', !titles.some((t) => t.includes('不该出现在面板里')), JSON.stringify(titles));

    const statuses = await page.$$eval('#tasksDialog .task-item .task-status', (nodes) => nodes.map((n) => n.textContent.trim()));
    check('状态显示为「停止中」', statuses.includes('停止中'), JSON.stringify(statuses));
    check('状态全为中文（没有漏出英文状态码）', statuses.every((s) => /[\u4e00-\u9fa5]/.test(s)), JSON.stringify(statuses));

    const cancelIds = await page.$$eval('#tasksDialog [data-task-cancel]', (nodes) => nodes.map((n) => n.dataset.taskCancel));
    check('运行中的作业有停止按钮', cancelIds.includes('smoke-job-running'), JSON.stringify(cancelIds));
    check('停止中的作业仍保留停止按钮', cancelIds.includes('smoke-job-stopping'), JSON.stringify(cancelIds));
    check('终态作业没有停止按钮', !cancelIds.includes('smoke-job-failed'), JSON.stringify(cancelIds));

    const summary = (await page.textContent('#taskSummary')) || '';
    check('统计条口径', /共 3 · 运行中 2 · 失败 1 · 已完成 0/.test(summary), summary);

    const detailButton = await page.$('#tasksDialog [data-task-detail="smoke-job-failed"]');
    check('失败作业有详情入口', Boolean(detailButton));
    if (detailButton) {
      await detailButton.click();
      const detailText = (await page.textContent('#tasksDialog [data-task-id="smoke-job-failed"] .task-detail')) || '';
      const visible = await page.isVisible('#tasksDialog [data-task-id="smoke-job-failed"] .task-detail');
      check('详情可展开且含失败原因', visible && detailText.includes('第 1 段提交失败'), detailText.slice(0, 120));
    }

    await page.setViewportSize({ width: 430, height: 900 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check('430px 视口无横向滚动', overflow <= 0, `overflow=${overflow}`);

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
