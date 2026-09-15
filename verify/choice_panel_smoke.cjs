// 交互选择面板冒烟（由 choice_panel_smoke.py 起独立源码 server 后调用）。
// 覆盖：面板挂载位置与「会话 + 来源消息」绑定 / 单选点击即进下一题 / 多选勾选与反选（不自动跳题）/
//       上一题·下一题回改且各题已选互不覆盖 / 折叠展开（已选保留）/ 草稿末尾追加答案块且不自动发送 /
//       待发送附件不被吞 / 切换会话不串题 / 重渲染与切回来都能恢复已选（内存键）/
//       点「完成」后同一来源消息不再弹面板 / 长题目与长选项不截断 / 零页面错误。
//
// 判据分工（与 regenerate_smoke.py 同口径）：
// - 面板的一切行为都由真前端在真实浏览器里点出来；后端只提供「带 choice_groups 的历史消息」这条真值。
// - `POST /api/chat` 全程拦截并计数：「点完成只填输入框、不发聊天请求」是真断言（计数必须为 0）。
// - `/api/providers/models` 也拦成「有目录」，是为了让发送前置校验成立——否则代码即使误发也会被
//   校验先行拦下，计数恒为 0，那条断言就成了假阳性。
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8797';
const SPEC = JSON.parse(process.env.NAIBA_SMOKE_SPEC || '{}');
const SHOTS = process.env.NAIBA_SMOKE_SHOTS || '';
const CONV = SPEC.conversations || {};
const MAIN = CONV.main || {};
const OTHER = CONV.other || {};
const LONG = CONV.long || {};
const MAIN_GROUPS = MAIN.groups || [];
const OTHER_GROUPS = OTHER.groups || [];
const LONG_GROUPS = LONG.groups || [];
const LONG_PROMPT = SPEC.long_prompt || '';
const LONG_CHOICES = SPEC.long_choices || [];
const BOUND_MODEL = SPEC.bound_model || '';
const ATTACHMENT = SPEC.attachment || '';
// 侧栏锚点：会话标题会被首条用户消息自动命名覆盖，所以点侧栏要用 sidebar 字段（首条用户消息）。
const SIDE_MAIN = MAIN.sidebar || MAIN.title;
const SIDE_OTHER = OTHER.sidebar || OTHER.title;
const SIDE_LONG = LONG.sidebar || LONG.title;
// 草稿里不能出现 `/` 与 `@`：它们会打开技能 / @ 引用浮层，干扰后续点击。
const DRAFT = '（草稿）先按这个方向做';

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}
/** 答案块口径与 `choiceAnswerBlock()` 同源：一题一行，多选同行用「、」连接。 */
function answerBlock(groups, picks) {
  return groups.map((group, index) => {
    const picked = (picks[index] || []).join('、');
    if (!picked) return '';
    return group.prompt ? `${group.prompt}：${picked}` : picked;
  }).filter(Boolean).join('\n');
}

(async () => {
  if (!MAIN.id || !OTHER.id || !LONG.id) {
    console.log(`FAIL  spec 不完整（应由 choice_panel_smoke.py 注入）-> ${JSON.stringify(Object.keys(CONV))}`);
    process.exit(1);
  }
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    // 浏览器会自动请求 /favicon.ico，服务端没有这条路由 → 必然 404；与页面逻辑无关。
    // 注意：404 的控制台文案本身不含 URL（"Failed to load resource..."），URL 在 location 里。
    const url = (msg.location() && msg.location().url) || '';
    if (url.includes('favicon') || msg.text().includes('favicon')) return;
    pageErrors.push(`console.error: ${msg.text()} @ ${url}`);
  });

  await page.route('**/api/providers/models', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ models: [{ id: BOUND_MODEL, name: BOUND_MODEL }] }),
    });
  });
  let chatRequests = 0;
  await page.route('**/api/chat', async (route) => {
    chatRequests += 1;
    await route.fulfill({
      status: 200, contentType: 'application/x-ndjson',
      body: `${JSON.stringify({ type: 'done', run_id: 'smoke-choice-run', sequence: 1 })}\n`,
    });
  });

  /** 面板快照：一次取全，断言只读快照（避免同一步里 DOM 已重绘导致前后不一致）。 */
  const panel = () => page.evaluate(() => {
    const host = document.querySelector('#choiceButtons');
    if (!host) return null;
    const one = (sel) => host.querySelector(sel);
    const body = one('.choice-body');
    const actions = one('.choice-actions');
    const badge = one('.choice-mode-badge');
    const collapse = one('.choice-collapse');
    return {
      key: host.dataset.choiceKey || '',
      role: host.getAttribute('role') || '',
      ariaLabel: host.getAttribute('aria-label') || '',
      collapsed: host.classList.contains('is-collapsed'),
      busy: host.classList.contains('is-busy'),
      inComposerWrap: Boolean(host.parentElement && host.parentElement.classList.contains('composer-wrap')),
      beforeComposerForm: host.nextElementSibling ? host.nextElementSibling.id : '',
      collapseGlyph: collapse ? collapse.textContent : '',
      collapseExpanded: collapse ? collapse.getAttribute('aria-expanded') : null,
      prompt: one('.choice-prompt') ? one('.choice-prompt').textContent : '',
      badge: badge ? badge.textContent : '',
      badgeClass: badge ? badge.className : '',
      progress: one('.choice-progress') ? one('.choice-progress').textContent : '',
      hint: one('.choice-hint') ? one('.choice-hint').textContent : '',
      bodyHidden: body ? body.hidden : null,
      actionsHidden: actions ? actions.hidden : null,
      summary: [...host.querySelectorAll('.choice-summary-item')].map((el) => el.textContent),
      summaryLabel: one('.choice-summary-label') ? one('.choice-summary-label').textContent : '',
      options: [...host.querySelectorAll('.choice-btn')].map((btn) => ({
        value: btn.dataset.choiceValue || '',
        selected: btn.classList.contains('is-selected'),
        checked: btn.getAttribute('aria-checked'),
        role: btn.getAttribute('role') || '',
        disabled: btn.disabled,
        hasCheck: Boolean(btn.querySelector('.choice-check')),
        text: btn.textContent,
      })),
      navs: [...host.querySelectorAll('[data-choice-nav]')].map((btn) => ({
        nav: btn.dataset.choiceNav || '', text: btn.textContent, disabled: btn.disabled,
      })),
    };
  });
  const optionOf = (snap, value) => (snap ? snap.options.find((o) => o.value === value) || null : null);
  const navOf = (snap, nav) => (snap ? snap.navs.find((n) => n.nav === nav) || null : null);
  const navKeys = (snap) => (snap ? snap.navs.map((n) => n.nav).join(',') : '');
  const inputValue = () => page.inputValue('#messageInput');
  const toastText = () => page.evaluate(() => (document.querySelector('#toast') || {}).textContent || '');
  const pendingFiles = () => page.evaluate(() => {
    const box = document.querySelector('#pendingFiles');
    if (!box) return null;
    const items = [...box.querySelectorAll('.pending-item')];
    return {
      hidden: box.hidden,
      count: items.length,
      uploading: items.some((el) => el.classList.contains('is-uploading')),
      names: items.map((el) => (el.querySelector('.pending-name') || {}).textContent || ''),
    };
  });
  async function waitFor(fn, timeout = 20000, step = 150) {
    const end = Date.now() + timeout;
    let last = null;
    while (Date.now() < end) {
      last = await fn();
      if (last) return last;
      await page.waitForTimeout(step);
    }
    return last;
  }
  const waitPanel = (timeout = 12000) => waitFor(async () => (await panel()) || null, timeout);
  const waitPanelGone = (timeout = 8000) => waitFor(async () => ((await panel()) === null ? true : null), timeout);
  /** 等面板落到满足条件的状态（渲染是「先移除后重建」，直接读会读到中间态）。 */
  const waitPanelWhere = (predicate, timeout = 6000) => waitFor(async () => {
    const snap = await panel();
    return snap && predicate(snap) ? snap : null;
  }, timeout);
  // 诊断行：失败时一眼看出卡在哪个会话、面板当时是什么状态。
  async function dump(label) {
    const snap = await panel();
    console.log(`  · ${label} | chatRequests=${chatRequests}`
      + ` panel=${snap ? JSON.stringify({
        key: String(snap.key).slice(-8), prompt: snap.prompt.slice(0, 12), progress: snap.progress,
        picked: snap.summary, navs: snap.navs.map((n) => `${n.nav}${n.disabled ? '(off)' : ''}`),
      }) : 'none'}`);
  }
  async function shot(name) {
    if (!SHOTS) return;
    fs.mkdirSync(SHOTS, { recursive: true });
    await page.screenshot({ path: path.join(SHOTS, name) });
  }
  /** 侧栏诊断：卡住时一眼看出「分组展开状态 / 行在不在虚拟窗口里 / 标题长什么样」。 */
  async function sidebarDump() {
    return page.evaluate(() => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      if (!tree) return { missing: true };
      return {
        groups: [...tree.querySelectorAll('.workspace-group')].map((el) => ({
          name: el.dataset.workspaceName || '(空)', expanded: el.classList.contains('expanded'),
        })),
        rows: tree.querySelectorAll('.conversation-item').length,
        titles: [...tree.querySelectorAll('.conversation-open')].map((el) => el.textContent),
        scrollTop: Math.round(tree.scrollTop), clientHeight: tree.clientHeight, scrollHeight: tree.scrollHeight,
      };
    });
  }
  // 侧栏是虚拟列表 + 分组默认折叠：**每次都要从顶部重新扫**（上一轮可能已经滚到底部，
  // 不重置就会立刻判「到底了」而漏掉上面新展开出来的行），滚到底还没出现就再展开一个分组。
  async function reveal(title) {
    await page.evaluate(() => { const tree = document.querySelector('#sidebarWorkspaceTree'); if (tree) tree.scrollTop = 0; });
    await page.waitForTimeout(160);
    for (let i = 0; i < 80; i += 1) {
      const state = await page.evaluate((needle) => {
        const tree = document.querySelector('#sidebarWorkspaceTree');
        const item = [...tree.querySelectorAll('.conversation-item')]
          .find((el) => (el.querySelector('.conversation-open')?.textContent || '').includes(needle));
        if (item) { item.scrollIntoView({ block: 'center' }); return 'found'; }
        const step = Math.max(120, tree.clientHeight * 0.8);
        if (tree.scrollTop + tree.clientHeight >= tree.scrollHeight - 1) return 'end';
        tree.scrollTop = Math.min(tree.scrollTop + step, tree.scrollHeight);
        return 'scroll';
      }, title);
      if (state === 'found') return true;
      if (state === 'end') return false;
      await page.waitForTimeout(140);
    }
    return false;
  }
  /** 展开一个仍折叠的工作区分组（表头 click 是委托到容器的，合成 click 即可）。 */
  async function expandOneGroup() {
    return page.evaluate(() => {
      const group = [...document.querySelectorAll('#sidebarWorkspaceTree .workspace-group')]
        .find((el) => !el.classList.contains('expanded'));
      if (!group) return false;
      group.querySelector('.workspace-group-header')?.click();
      return true;
    });
  }
  async function openByTitle(title) {
    let opened = false;
    for (let i = 0; i < 12; i += 1) {
      if (await reveal(title)) { opened = true; break; }
      if (!(await expandOneGroup())) break;
      await page.waitForTimeout(240);
    }
    if (!opened) {
      console.log(`  · 侧栏诊断（找不到「${title}」）-> ${JSON.stringify(await sidebarDump())}`);
      return false;
    }
    await page.waitForTimeout(120);
    await page.locator('#sidebarWorkspaceTree .conversation-open', { hasText: title }).first().click();
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 20000 });
    await page.waitForTimeout(400);
    return true;
  }
  async function clickOption(value) {
    const index = await page.evaluate((needle) => [...document.querySelectorAll('#choiceButtons .choice-btn')]
      .findIndex((btn) => (btn.dataset.choiceValue || '') === needle), value);
    if (index < 0) return false;
    await page.locator('#choiceButtons .choice-btn').nth(index).click();
    await page.waitForTimeout(160);
    return true;
  }
  async function clickNav(nav) {
    await page.locator(`#choiceButtons [data-choice-nav="${nav}"]`).click();
    await page.waitForTimeout(160);
  }

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });

    // ---- ① 打开两题会话：面板从历史元数据恢复，并绑定「会话 + 来源消息」 ----
    check('点侧栏打开两题会话（最近更新的会话启动时已自动打开）', await openByTitle(SIDE_MAIN), MAIN.title);
    const first = await waitPanel();
    await dump('① main 首题');
    await shot('01_main_question1.png');
    check('打开会话即从历史弹出选择面板', !!first);
    check('面板挂在 composer-wrap 里、紧贴输入框上方（不进消息流）',
      !!first && first.inComposerWrap === true && first.beforeComposerForm === 'composerForm',
      JSON.stringify(first && { inWrap: first.inComposerWrap, next: first.beforeComposerForm }));
    check('面板 role=group 且 aria-label 用题面',
      !!first && first.role === 'group' && first.ariaLabel === MAIN_GROUPS[0].prompt,
      JSON.stringify(first && { role: first.role, label: first.ariaLabel }));
    check('面板绑定「会话 + 来源消息」（换消息不串题）',
      !!first && first.key === `${MAIN.id}::${MAIN.message_id}`,
      JSON.stringify(first && { key: first.key, want: `${MAIN.id}::${MAIN.message_id}` }));
    check('第一题题面 / 单选标识 / 进度 / 提示',
      !!first && first.prompt === '视觉风格' && first.badge === '单选'
      && first.badgeClass.includes('is-single') && first.progress === '1/2' && first.hint === '请选择一项',
      JSON.stringify(first && { p: first.prompt, b: first.badge, c: first.badgeClass, g: first.progress, h: first.hint }));
    check('第一题列出全部选项且是 radio、初始未选',
      !!first && JSON.stringify(first.options.map((o) => o.value)) === JSON.stringify(MAIN_GROUPS[0].choices)
      && first.options.every((o) => o.role === 'radio' && o.checked === 'false' && !o.hasCheck && !o.selected),
      JSON.stringify(first && first.options));
    check('未选择时「下一题」禁用、且此时没有「上一题」',
      !!first && navKeys(first) === 'collapse,next'
      && navOf(first, 'next').text === '下一题' && navOf(first, 'next').disabled === true,
      JSON.stringify(first && first.navs));

    // ---- ② 单选点击即进入下一题（不会停在原地等「完成」） ----
    check('点第一题选项', await clickOption(MAIN_GROUPS[0].choices[0]));
    const second = await waitPanelWhere((snap) => snap.progress === '2/2');
    await dump('② main 第二题');
    check('单选点击后自动进入第二题', !!second, JSON.stringify(second && second.progress));
    check('第二题题面 / 多选标识 / 已选项带勾选框',
      !!second && second.prompt === MAIN_GROUPS[1].prompt && second.badge === '多选'
      && second.badgeClass.includes('is-multi')
      && JSON.stringify(second.options.map((o) => o.value)) === JSON.stringify(MAIN_GROUPS[1].choices)
      && second.options.every((o) => o.role === 'checkbox' && o.hasCheck),
      JSON.stringify(second && { p: second.prompt, b: second.badge, o: second.options.map((x) => [x.role, x.hasCheck]) }));
    check('最后一题主按钮变「完成」、出现「上一题」，未勾选时完成仍禁用',
      !!second && navKeys(second) === 'collapse,prev,done'
      && navOf(second, 'done').text === '完成' && navOf(second, 'done').disabled === true
      && navOf(second, 'prev').text === '上一题' && navOf(second, 'prev').disabled === false,
      JSON.stringify(second && second.navs));

    // ---- ③ 多选：勾选 / 反选，且勾选不自动跳题 ----
    const [multiA, multiB] = MAIN_GROUPS[1].choices;
    await clickOption(multiA);
    const picked1 = await waitPanelWhere((snap) => snap.summary.length === 1);
    check('勾选第一项后仍停在第二题（多选不自动跳题）',
      !!picked1 && picked1.progress === '2/2' && picked1.prompt === MAIN_GROUPS[1].prompt,
      JSON.stringify(picked1 && { g: picked1.progress, p: picked1.prompt }));
    check('勾选项标记为已选，并给出「已选 N 项」与已选摘要',
      !!picked1 && optionOf(picked1, multiA).selected === true && optionOf(picked1, multiA).checked === 'true'
      && picked1.hint === '已选 1 项' && picked1.summaryLabel === '已选：'
      && JSON.stringify(picked1.summary) === JSON.stringify([multiA]),
      JSON.stringify(picked1 && { o: optionOf(picked1, multiA), h: picked1.hint, s: picked1.summary }));
    check('多选后「完成」按钮解禁', !!picked1 && navOf(picked1, 'done').disabled === false,
      JSON.stringify(picked1 && navOf(picked1, 'done')));
    await clickOption(multiB);
    const picked2 = await waitPanelWhere((snap) => snap.summary.length === 2);
    check('多选可继续追加（已选 2 项，摘要逐项列出）',
      !!picked2 && picked2.hint === '已选 2 项'
      && JSON.stringify(picked2.summary) === JSON.stringify([multiA, multiB]),
      JSON.stringify(picked2 && { h: picked2.hint, s: picked2.summary }));
    await clickOption(multiB);
    const unpicked = await waitPanelWhere((snap) => snap.summary.length === 1);
    check('再点一次可反选（取消勾选，摘要同步收敛）',
      !!unpicked && optionOf(unpicked, multiB).selected === false && optionOf(unpicked, multiB).checked === 'false'
      && unpicked.hint === '已选 1 项' && JSON.stringify(unpicked.summary) === JSON.stringify([multiA]),
      JSON.stringify(unpicked && { o: optionOf(unpicked, multiB), h: unpicked.hint, s: unpicked.summary }));
    await clickOption(multiB);
    check('重新勾回第二项（答案块用两选）',
      (await waitPanelWhere((snap) => snap.summary.length === 2)) !== null);

    // ---- ④ 上一题 / 下一题：回改不覆盖其他题已选 ----
    await clickNav('prev');
    const back = await waitPanelWhere((snap) => snap.progress === '1/2');
    check('「上一题」回到第一题，进度与题面同步',
      !!back && back.prompt === '视觉风格' && back.badge === '单选',
      JSON.stringify(back && { g: back.progress, p: back.prompt }));
    check('回到第一题后原选项仍是选中态',
      !!back && optionOf(back, MAIN_GROUPS[0].choices[0]).selected === true,
      JSON.stringify(back && back.options));
    await clickOption(MAIN_GROUPS[0].choices[1]);
    const changed = await waitPanelWhere((snap) => snap.progress === '2/2');
    check('回改第一题后自动回到第二题，第二题已选不受影响',
      !!changed && JSON.stringify(changed.summary) === JSON.stringify([multiA, multiB]),
      JSON.stringify(changed && changed.summary));
    await clickNav('prev');
    const afterChange = await waitPanelWhere((snap) => snap.progress === '1/2');
    check('回改结果写进了面板状态（新选项选中、旧选项取消，单选只留一个）',
      !!afterChange && optionOf(afterChange, MAIN_GROUPS[0].choices[1]).selected === true
      && optionOf(afterChange, MAIN_GROUPS[0].choices[0]).selected === false,
      JSON.stringify(afterChange && afterChange.options));
    await clickNav('next');
    const forwarded = await waitPanelWhere((snap) => snap.progress === '2/2');
    check('「下一题」推进一格且不改动已选',
      !!forwarded && JSON.stringify(forwarded.summary) === JSON.stringify([multiA, multiB]),
      JSON.stringify(forwarded && forwarded.summary));
    await clickNav('prev');
    await waitPanelWhere((snap) => snap.progress === '1/2');
    await clickOption(MAIN_GROUPS[0].choices[0]);
    const restored = await waitPanelWhere((snap) => snap.progress === '2/2');
    check('改回第一题原选项后答案仍为两题齐全',
      !!restored && JSON.stringify(restored.summary) === JSON.stringify([multiA, multiB]),
      JSON.stringify(restored && restored.summary));

    // ---- ⑤ 折叠 / 展开：只影响显示，已选照样保留 ----
    await clickNav('collapse');
    const folded = await waitPanelWhere((snap) => snap.collapsed === true);
    check('折叠后选项与操作区一起隐藏（题面与进度仍在）',
      !!folded && folded.bodyHidden === true && folded.actionsHidden === true
      && folded.progress === '2/2' && folded.collapseExpanded === 'false' && folded.collapseGlyph === '▸',
      JSON.stringify(folded && { c: folded.collapsed, b: folded.bodyHidden, a: folded.actionsHidden, g: folded.progress }));
    await clickNav('collapse');
    const unfolded = await waitPanelWhere((snap) => snap.collapsed === false);
    check('展开后保持折叠前的进度与已选（折叠不是重置）',
      !!unfolded && unfolded.bodyHidden === false && unfolded.progress === '2/2'
      && JSON.stringify(unfolded.summary) === JSON.stringify([multiA, multiB])
      && optionOf(unfolded, multiA).selected === true,
      JSON.stringify(unfolded && { c: unfolded.collapsed, g: unfolded.progress, s: unfolded.summary }));

    // ---- ⑥ 待发送附件 + 草稿 + 「完成」：答案追加进输入框、不发请求 ----
    await page.setInputFiles('#fileInput', ATTACHMENT);
    const attached = await waitFor(async () => {
      const snap = await pendingFiles();
      return snap && snap.count === 1 && !snap.uploading ? snap : null;
    }, 30000);
    check('附件上传完成后出现在待发送列表',
      !!attached && attached.hidden === false && attached.names[0].includes('smoke-choice-att'),
      JSON.stringify(attached));
    await page.fill('#messageInput', DRAFT);
    await clickNav('done');
    const gone = await waitPanelGone();
    await dump('⑥ main 完成');
    await shot('02_main_completed_input.png');
    const expectedInput = `${DRAFT}\n${answerBlock(MAIN_GROUPS, [[MAIN_GROUPS[0].choices[0]], [multiA, multiB]])}`;
    check('「完成」后面板消失', gone === true);
    check('答案块以换行追加在草稿末尾（草稿没被替换掉）',
      (await inputValue()) === expectedInput, JSON.stringify(await inputValue()));
    check('完成只填输入框、不自动发送（/api/chat 计数为 0）', chatRequests === 0, String(chatRequests));
    check('完成给出「确认后再发送」提示',
      (await toastText()).includes('选择已填入输入框，确认后再发送'), await toastText());
    check('待发送附件不被答案块挤掉',
      JSON.stringify(await pendingFiles()) === JSON.stringify(attached), JSON.stringify(await pendingFiles()));

    // ---- ⑦ 切换会话不串题：另一会话弹的是它自己的题 ----
    await page.fill('#messageInput', '');
    await page.locator('#pendingFiles .pending-remove').first().click();
    await page.waitForTimeout(250);
    check('移除附件后待发送列表清空', (await pendingFiles()).count === 0, JSON.stringify(await pendingFiles()));
    check('切到单题会话', await openByTitle(SIDE_OTHER), OTHER.title);
    const otherPanel = await waitPanel();
    await dump('⑦ other 单题');
    check('单题会话弹出的是自己的题面与选项（不串题）',
      !!otherPanel && otherPanel.key === `${OTHER.id}::${OTHER.message_id}`
      && otherPanel.prompt === '推送渠道' && otherPanel.badge === '单选'
      && JSON.stringify(otherPanel.options.map((o) => o.value)) === JSON.stringify(OTHER_GROUPS[0].choices),
      JSON.stringify(otherPanel && { key: otherPanel.key, p: otherPanel.prompt, o: otherPanel.options.map((o) => o.value) }));
    check('只有一组题时不显示进度，也没有「上一题」',
      !!otherPanel && otherPanel.progress === '' && !navOf(otherPanel, 'prev')
      && navKeys(otherPanel) === 'collapse,done',
      JSON.stringify(otherPanel && { g: otherPanel.progress, n: navKeys(otherPanel) }));
    check('另一会话初始没有任何已选',
      !!otherPanel && otherPanel.summary.length === 0 && otherPanel.hint === '请选择一项',
      JSON.stringify(otherPanel && { s: otherPanel.summary, h: otherPanel.hint }));
    await clickOption(OTHER_GROUPS[0].choices[2]);
    const otherPicked = await waitPanelWhere((snap) => snap.summary.length === 1);
    check('单题点选后停在原地（等统一「完成」）',
      !!otherPicked && otherPicked.progress === '' && navOf(otherPicked, 'done').disabled === false
      && JSON.stringify(otherPicked.summary) === JSON.stringify([OTHER_GROUPS[0].choices[2]]),
      JSON.stringify(otherPicked && { g: otherPicked.progress, s: otherPicked.summary }));

    // ---- ⑧ 切回已完成的会话：面板不再弹（对同一条历史真值失效） ----
    check('切回两题会话', await openByTitle(SIDE_MAIN), MAIN.title);
    check('已完成的题不会因重新加载历史而再弹面板',
      (await waitPanelGone(3000)) === true, JSON.stringify(await panel()));
    check('切回来也不会把答案块再填一遍（输入框没被改）',
      (await inputValue()) === '', JSON.stringify(await inputValue()));

    // ---- ⑨ 切回未完成的会话：内存键保留已选 ----
    check('再切回单题会话', await openByTitle(SIDE_OTHER), OTHER.title);
    const resumed = await waitPanelWhere((snap) => snap.summary.length === 1);
    check('未完成的会话切回来重新挂载面板，已选仍在（内存键保留）',
      !!resumed && resumed.key === `${OTHER.id}::${OTHER.message_id}`
      && optionOf(resumed, OTHER_GROUPS[0].choices[2]).selected === true
      && resumed.hint === '已选 1 项',
      JSON.stringify(resumed && { key: resumed.key, s: resumed.summary, h: resumed.hint }));

    // ---- ⑩ 没有草稿时「完成」：输入框就是答案块本身（不带前导换行） ----
    await clickNav('done');
    check('无草稿完成后面板消失', (await waitPanelGone()) === true);
    check('无草稿时输入框是该会话自己的答案块（题面：选项）',
      (await inputValue()) === answerBlock(OTHER_GROUPS, [[OTHER_GROUPS[0].choices[2]]]),
      JSON.stringify(await inputValue()));
    check('至此仍然没有发出任何聊天请求', chatRequests === 0, String(chatRequests));
    check('再切走再切回仍不弹面板（失效是持久的）',
      (await openByTitle(SIDE_MAIN)) === true && (await waitPanelGone(3000)) === true
      && (await openByTitle(SIDE_OTHER)) === true && (await waitPanelGone(3000)) === true,
      JSON.stringify(await panel()));

    // ---- ⑪ 长题目 / 长选项不截断（本次改造放宽上限的诉求） ----
    await page.fill('#messageInput', '');
    check('打开长文本会话', await openByTitle(SIDE_LONG), LONG.title);
    const longPanel = await waitPanelWhere((snap) => snap.prompt.length > 100);
    await dump('⑪ long');
    await shot('03_long_text_panel.png');
    check('长题目完整渲染（既没被上限过滤掉、也没被截断）',
      !!longPanel && longPanel.prompt === LONG_PROMPT,
      JSON.stringify(longPanel && { got: longPanel.prompt.length, want: LONG_PROMPT.length }));
    check('长选项完整渲染',
      !!longPanel && JSON.stringify(longPanel.options.map((o) => o.text)) === JSON.stringify(LONG_CHOICES),
      JSON.stringify(longPanel && longPanel.options.map((o) => o.text.length)));
    check('长题目的 aria-label 同样是完整题面',
      !!longPanel && longPanel.ariaLabel === LONG_PROMPT, String(longPanel && longPanel.ariaLabel.length));
    await clickOption(LONG_CHOICES[1]);
    const longPicked = await waitPanelWhere((snap) => snap.summary.length === 1);
    check('长选项选中后摘要也完整（不截断）',
      !!longPicked && longPicked.summary[0] === LONG_CHOICES[1],
      JSON.stringify(longPicked && longPicked.summary.map((text) => text.length)));
    await clickNav('done');
    const longExpected = answerBlock(LONG_GROUPS, [[LONG_CHOICES[1]]]);
    check('长文本答案块完整进入输入框',
      (await waitFor(async () => ((await inputValue()) === longExpected ? true : null), 6000)) === true,
      JSON.stringify((await inputValue()).length));
    await shot('04_long_text_input.png');

    // ---- ⑫ 全程零页面错误 ----
    check('全程零 pageerror / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
