// 工具分类改版冒烟（随仓库发布的可复用资产；由 tool_groups_smoke.py 起**隔离实例**后调用——
// 独立 config / data_dir / 端口，避开在跑窗口的单实例锁，也让 ⑩ 段落播种的 Agent 与工具集
// 只落在临时目录里）。
// 2026-11-07 随「Agent 管理页重设计」按新契约重写：模板区不再是卡片网格，而是
// `<select>` + `<optgroup>`（内置预设 / 我的工具集两组）；下拉是**动作入口**——用
// page.selectOption 送 `preset:<id>` / `template:<id>`，选完立即复位空占位「套用工具集模板…」；
// `#agentToolPresetView` 与 `#agentToolEditor` **同屏常显**（不再两态互斥 hidden）；
// `#editAgentToolScope` / `#agentToolEditorBack` 两个纯导航按钮已删除（层级压平后无第三层可进）；
// 只读清单 `#agentToolPeekList` 默认收起，按钮初始「展开清单」。
// 载入模板可能弹原生 confirm（「未保存的自定义组合」防误触，确认文案钉在
// confirmToolScopeOverwrite()）——page.on('dialog') 做成可切换处理器：默认 accept，
// 防误触 / 删除引用提示两处先 dismiss 断言再放行。
// 覆盖：下拉占位 + 5 个内置预设 option + optgroup「我的工具集」/ 选模板套用后 select 复位占位 /
//       命名栏预填模板名 / 「添加自定义工具集」独立按钮以**当前勾选**为底稿且不弹确认 /
//       两态同屏（两个容器同时可见、无 is-picked / is-active / .tool-preset-card 残留）/
//       保存「我的工具集」与复用、独立「删除工具集」按钮（含「谁在用」引用提示）/
//       自定义未保存时选模板先 confirm（取消=分毫不动）/
//       6 组顺序 / 风险徽标与配色 / 徽标与分类名同行 / 说明非空 / 展开收起 /
//       分类级全选与计数 / 搜索框过滤与恢复 / MCP 按服务器二级分组与二级全选 / 零页面错误。
// 运行：.venv\Scripts\python.exe verify\tool_groups_smoke.py
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8793';
const failures = [];
let passes = 0;
function check(label, ok, detail = '') {
  if (ok) passes += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

const EXPECTED_GROUPS = ['读取与检索', '文件写入与编辑', '命令与脚本执行', '联网与外部服务', '视觉与图片', '任务与扩展', '长会话'];
const EXPECTED_BADGES = {
  '读取与检索': '只读',
  '文件写入与编辑': '会改文件',
  '命令与脚本执行': '高风险',
  '联网与外部服务': '联网',
  '视觉与图片': '会写产物',
  '任务与扩展': '会改动',
  '长会话': '会话控制',
};
const RETIRED_GROUPS = ['文件读取/搜索', '文件写入/编辑', '命令执行', 'Skill 脚本', '网络', '会话与记忆',
  'MCP', '后台/Job/子任务', 'ComfyUI', '能力/Skill 管理', '文档（PDF）', '视觉'];

async function scopeSnapshot(page) {
  return page.evaluate(() => {
    const scope = document.querySelector('#agentToolScope');
    const groups = [...scope.querySelectorAll('.agent-tool-group')];
    return {
      hasFilter: Boolean(document.querySelector('#agentToolFilter')),
      groupCount: groups.length,
      groups: groups.map((group) => {
        const title = group.querySelector('.group-title');
        const badge = group.querySelector('.group-badge');
        const desc = group.querySelector('.group-desc');
        const head = group.querySelector('.agent-tool-group-head');
        const titleRect = title.getBoundingClientRect();
        const badgeRect = badge ? badge.getBoundingClientRect() : null;
        return {
          name: group.dataset.group,
          titleText: title.childNodes[0]?.textContent.trim() || '',
          badge: badge ? badge.textContent.trim() : '',
          tone: badge ? badge.dataset.tone : '',
          badgeInline: Boolean(badgeRect) && Math.abs(badgeRect.top - titleRect.top) < 8
            && badgeRect.left >= titleRect.left,
          desc: desc ? desc.textContent.trim() : '',
          collapsed: group.classList.contains('collapsed'),
          bodyHidden: Boolean(group.querySelector('.agent-tool-group-body')?.hidden),
          toolCards: group.querySelectorAll('.permission-grid input[type="checkbox"]').length,
          count: group.querySelector('.group-count')?.textContent.trim() || '',
          headHeight: Math.round(head.getBoundingClientRect().height),
        };
      }),
    };
  });
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  // 全新数据目录（隔离实例）首启会弹 #onboardingDialog 挡住所有点击；真 config 下它已被忽略。
  // 与其它冒烟同款做法：启动前置上引导标记；节点本身摘掉（没有可用供应商时引导会自己再开，实测踩过）。
  await page.addInitScript(() => {
    try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (error) { void error; }
  });
  const pageErrors = [];
  const badResponses = [];
  // ⑩ 段落播种出来的 Agent / 工具集 id：收尾（含异常路径）一律删掉。
  let seedAgents = [];
  let seedSets = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    // 「Failed to load resource」这条文案不带 URL，没法与已知噪音区分；接口层的 4xx/5xx
    // 交给下面的 badResponses 逐条带 URL 断言，所以这里只收真正的 console.error 文案。
    if (msg.type() === 'error' && !msg.text().includes('Failed to load resource')) {
      pageErrors.push(`console.error: ${msg.text()}`);
    }
  });
  // 已知取样噪音：隔离实例（verify/_serve_tmp.py）里种的是占位供应商 `https://example.invalid/v1`，
  // 设置页的「检测模型目录」必然连不上 → 后端回 400。这是 harness 的属性，不是页面问题；
  // 其余任何 4xx/5xx 照旧算失败。
  const KNOWN_NOISE = /\/api\/providers\/models$/;
  page.on('response', (res) => {
    if (res.status() >= 400 && !KNOWN_NOISE.test(res.url())) {
      badResponses.push(`${res.status()} ${res.url()}`);
    }
  });
  // 原生 confirm：默认一律 accept（既有场景都不关心内容）；防误触 / 删除引用提示这两处场景
  // 需要「先 dismiss，看勾选有没有被改」再把消息拿去断言，所以做成可切换的处理器。
  const dialogs = [];
  let dialogHandler = (dialog) => dialog.accept();
  page.on('dialog', (dialog) => {
    dialogs.push(dialog.message());
    return dialogHandler(dialog);
  });

  // 打开某张 Agent 卡 → 弹层 → 切到「工具集」页，等下拉里出现 5 个内置预设 option。
  const openAgentForm = async (id) => {
    if (id) {
      await page.click(`[data-agent-card="${id}"] .agent-card-name`);
    } else {
      await page.click('#agentCards .agent-card');
    }
    await page.waitForSelector('#agentDialog[open]', { timeout: 10000 });
    await page.waitForTimeout(500);
    await page.click('[data-agent-tab="tools"]');
    await page.waitForTimeout(300);
    await page.waitForFunction(
      () => document.querySelectorAll('#agentToolPresetCards option[data-tool-preset-card]').length >= 5,
      null, { timeout: 10000 },
    ).catch(() => {});
    await page.waitForTimeout(200);
  };
  const closeAgentForm = async () => {
    await page.click('#cancelAgent');
    await page.waitForTimeout(500);
  };
  // 同屏断言的公共取数：两个容器都必须「没有 hidden」。
  const stageVisibility = () => page.evaluate(() => ({
    viewVisible: document.querySelector('#agentToolPresetView')?.hidden === false,
    editorVisible: document.querySelector('#agentToolEditor')?.hidden === false,
    selectValue: document.querySelector('#agentToolPresetCards')?.value ?? '',
  }));

  try {
    // ① 接口数据：分组顺序 / 徽标 / 二级分组字段齐备
    const catalog = await (await fetch(`${BASE}/api/tool_catalog`)).json();
    const apiGroups = catalog.groups || [];
    // 「全套」预设的个数不能拿 catalog.tools.length 当基准：全能模式显式排除了互斥的后位子代理工具
    // （naiba/config.py 的 TOOL_PRESETS full.exclude = ["subagent_spawn"]），也永远不带 opt-in 的
    // extract_video_frames_exact（预设解析统一剔除，config.py resolve_tool_preset）。
    // 唯一权威是接口自己解析好的 preset.tools —— 下拉 option 显示的个数必须等于它。
    const presetToolCount = (id) => {
      const preset = (catalog.presets || []).find((p) => p.id === id);
      return (preset?.tools || []).length;
    };
    check('接口返回 7 个分类', apiGroups.length === 7, JSON.stringify(apiGroups.map((g) => g.name)));
    check('分类顺序与设计一致', JSON.stringify(apiGroups.map((g) => g.name)) === JSON.stringify(EXPECTED_GROUPS),
      JSON.stringify(apiGroups.map((g) => g.name)));
    check('每组都带风险徽标与配色', apiGroups.every((g) => g.badge && g.tone),
      JSON.stringify(apiGroups.map((g) => [g.name, g.badge, g.tone])));
    check('每组都有说明小字', apiGroups.every((g) => String(g.desc || '').trim()),
      JSON.stringify(apiGroups.map((g) => [g.name, g.desc])));
    check('每组都有 direct_tools / subgroups 字段',
      apiGroups.every((g) => Array.isArray(g.direct_tools) && Array.isArray(g.subgroups)), '');
    const names = apiGroups.map((g) => g.name);
    check('旧分类名不再出现', RETIRED_GROUPS.every((name) => !names.includes(name)),
      JSON.stringify(names.filter((name) => RETIRED_GROUPS.includes(name))));
    const allTools = apiGroups.flatMap((g) => g.tools);
    check('每个工具只归入一个分类', allTools.length === new Set(allTools).size && allTools.length === (catalog.tools || []).length,
      JSON.stringify({ placed: allTools.length, unique: new Set(allTools).size, total: (catalog.tools || []).length }));

    // ② 打开设置 → Agent → 卡片 → 弹层
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(800);
    await page.evaluate(() => document.querySelector('#onboardingDialog')?.remove());
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForTimeout(400);
    await openAgentForm(null);

    // ②.5 新契约初始态：下拉 + 分组清单同屏；已删按钮确凿不存在；旧卡片类名零残留。
    const initial = await page.evaluate(() => {
      const select = document.querySelector('#agentToolPresetCards');
      const presets = [...select.querySelectorAll('option[data-tool-preset-card]')].map((opt) => ([
        opt.dataset.toolPresetCard,
        opt.textContent.trim(),
      ]));
      const templates = [...select.querySelectorAll('option[data-tool-template-card]')];
      const group = (label) => {
        const og = [...select.querySelectorAll('optgroup')].find((el) => el.label === label);
        return { present: Boolean(og), options: og ? og.querySelectorAll('option').length : 0 };
      };
      return {
        isSelect: select.tagName === 'SELECT',
        placeholder: select.querySelector('option[value=""]')?.textContent.trim() || '',
        selectValue: select.value,
        presets,
        templateCount: templates.length,
        builtinGroup: group('内置预设'),
        mineGroup: group('我的工具集'),
        oldCardDom: Boolean(document.querySelector('.tool-preset-card')),
        pickedLeftovers: document.querySelectorAll('#agentToolStage .is-picked, #agentToolPresetCards.is-active').length,
        removedButtons: {
          editScope: Boolean(document.querySelector('#editAgentToolScope')),
          back: Boolean(document.querySelector('#agentToolEditorBack')),
        },
        viewVisible: document.querySelector('#agentToolPresetView')?.hidden === false,
        editorVisible: document.querySelector('#agentToolEditor')?.hidden === false,
        delHidden: document.querySelector('#deleteAgentToolTemplate')?.hidden !== false,
        peekHidden: document.querySelector('#agentToolPeekList')?.hidden !== false,
        peekToggle: document.querySelector('#toggleAgentToolPeek')?.textContent.trim() || '',
        peekAria: document.querySelector('#toggleAgentToolPeek')?.getAttribute('aria-expanded') || '',
        summary: document.querySelector('#agentToolPresetState')?.textContent.trim() || '',
        dialogH: Math.round(document.querySelector('#agentDialog')?.getBoundingClientRect().height || 0),
      };
    });
    check('模板选择器是 <select> 且收起时停在占位「套用工具集模板…」',
      initial.isSelect && initial.placeholder === '套用工具集模板…' && initial.selectValue === '',
      JSON.stringify({ isSelect: initial.isSelect, placeholder: initial.placeholder, value: initial.selectValue }));
    // 个数权威口径 = naiba/config.py 的 TOOL_PRESETS 出厂定义（经 resolve_tool_preset 解析：
    // 剔除 opt-in、comfyui 档再剔 MCP），由 /api/tool_catalog 的 preset.tools 下发；前端下拉
    // option 文案就是 `${preset.name} · ${preset.tools.length} 个工具`（09-settings.js
    // renderAgentToolPresetCards，state.toolCatalog.presets 直出）。逐档对账：
    //   readonly    9 = config.py:1255-1261（纯读取六件 + 拆书匠 novel_outline/novel_chunks/novel_split_text + vision_analyze）
    //   standard   21 = config.py:1268-1278（含后台五件套 run_in_background/job_output/job_status/job_wait/job_kill）
    //   longsession 25 = config.py:1285-1294（standard 全部 + 翻历史/重置上下文四件）
    //   comfyui    23 = config.py:1301-1314（standard 主体 + comfyui_prepare_workflow/comfyui_batch + 任务闭包）
    //   full      动态 = config.py:1322 group:* − exclude subagent_spawn − opt-in，随目录规模变，仍取接口解析值。
    check('内置预设 optgroup 里 5 个 option 名称与个数正确',
      JSON.stringify(initial.presets)
      === JSON.stringify([
        ['readonly', '只读模式 · 9 个工具'], ['standard', '标准模式 · 21 个工具'],
        ['longsession', '长会话模式 · 25 个工具'], ['comfyui', 'ComfyUI 联动 · 23 个工具'],
        ['full', `全能模式 · ${presetToolCount('full')} 个工具`],
      ]), JSON.stringify(initial.presets));
    check('初始没有「我的工具集」option（后端还没有保存过工具集）',
      initial.templateCount === 0 && initial.mineGroup.present === false && initial.builtinGroup.options === 5,
      JSON.stringify(initial));
    check('旧卡片 DOM 零残留（无 .tool-preset-card 类、无 is-picked / is-active）',
      initial.oldCardDom === false && initial.pickedLeftovers === 0, JSON.stringify(initial));
    check('「编辑当前工具集」「返回」两个按钮已整体移除',
      initial.removedButtons.editScope === false && initial.removedButtons.back === false,
      JSON.stringify(initial.removedButtons));
    check('下拉区与分组勾选清单同屏常显（都不带 hidden）',
      initial.viewVisible === true && initial.editorVisible === true, JSON.stringify(initial));
    check('未载入模板时「删除工具集」按钮隐藏（防误删）', initial.delHidden === true, String(initial.delHidden));
    check('只读清单默认收起（按钮「展开清单」、aria-expanded=false）',
      initial.peekHidden === true && initial.peekToggle === '展开清单' && initial.peekAria === 'false',
      JSON.stringify({ peekHidden: initial.peekHidden, toggle: initial.peekToggle, aria: initial.peekAria }));
    check('摘要行按「当前：…」格式显示', initial.summary.startsWith('当前：'), initial.summary);
    check('弹层高度固定（>600px，不随内容跳）', initial.dialogH > 600, String(initial.dialogH));
    const dialogH = initial.dialogH;

    // 只读清单开合一轮：展开 → 「收起清单」→ 再收起复位。
    await page.click('#toggleAgentToolPeek');
    await page.waitForTimeout(250);
    const peekOpen = await page.evaluate(() => ({
      hidden: document.querySelector('#agentToolPeekList')?.hidden !== false,
      toggle: document.querySelector('#toggleAgentToolPeek')?.textContent.trim() || '',
      aria: document.querySelector('#toggleAgentToolPeek')?.getAttribute('aria-expanded') || '',
    }));
    check('点一次后清单展开（按钮变「收起清单」、aria-expanded=true）',
      peekOpen.hidden === false && peekOpen.toggle === '收起清单' && peekOpen.aria === 'true',
      JSON.stringify(peekOpen));
    await page.click('#toggleAgentToolPeek');
    await page.waitForTimeout(250);
    const peekClosed = await page.evaluate(() => ({
      hidden: document.querySelector('#agentToolPeekList')?.hidden !== false,
      toggle: document.querySelector('#toggleAgentToolPeek')?.textContent.trim() || '',
    }));
    check('再点一次清单收起复位（按钮回「展开清单」）',
      peekClosed.hidden === true && peekClosed.toggle === '展开清单', JSON.stringify(peekClosed));

    // ②.6 载入内置预设：下拉选 `preset:<id>` → 命名栏预填、勾选换成该预设、下拉复位占位。
    // 首个内置 Agent 的出厂工具集多为「预设 + 指名出卡」（精确匹配口径算「未保存的自定义组合」），
    // 第一次套用会弹一次防误触 confirm —— 默认 accept，让载入生效。
    const PRESET_NAMES = { readonly: '只读模式', standard: '标准模式', comfyui: 'ComfyUI 联动', full: '全能模式' };
    // 套用后的勾选数必须等于下拉 option 显示的个数：预设已自带依赖闭包（config.py:1240-1241
    // 的硬约束），前端 openAgentToolEditor 只做 normalizeToolScope（补依赖/互斥归一），不会再变数。
    // readonly 9 / standard 21 / comfyui 23（config.py:1255-1261 / 1268-1278 / 1301-1314）；
    // full 动态。摘要行文案 = 09-settings.js toolSetSummary() 的
    // `当前：${matched.name} · ${total} 个工具`（displayScopeMatch 精确命中预设名）。
    for (const [value, expected] of [['readonly', 9], ['standard', 21], ['comfyui', 23],
      ['full', presetToolCount('full')]]) {
      await page.selectOption('#agentToolPresetCards', `preset:${value}`);
      await page.waitForTimeout(500);
      const loaded = await page.evaluate(() => ({
        name: document.querySelector('#agentToolSetName')?.value || '',
        checked: document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked').length,
        selectValue: document.querySelector('#agentToolPresetCards')?.value ?? '',
        summary: document.querySelector('#agentToolPresetState')?.textContent.trim() || '',
        dialogH: Math.round(document.querySelector('#agentDialog')?.getBoundingClientRect().height || 0),
      }));
      check(`下拉选「${value}」后命名栏自动预填「${PRESET_NAMES[value]}」`,
        loaded.name === PRESET_NAMES[value], JSON.stringify(loaded));
      check(`载入「${value}」的工具个数 == 下拉 option 显示个数（${expected}）`,
        loaded.checked === expected, JSON.stringify(loaded));
      check(`选完「${value}」下拉立即复位空占位（不保留选中态）`, loaded.selectValue === '', loaded.selectValue);
      const vis = await stageVisibility();
      check(`载入「${value}」后下拉区与清单仍同屏`, vis.viewVisible && vis.editorVisible, JSON.stringify(vis));
      check(`载入「${value}」后弹层高度不变（${dialogH}px）`, Math.abs(loaded.dialogH - dialogH) <= 2, String(loaded.dialogH));
      check(`摘要行跟随为「当前：${PRESET_NAMES[value]} · ${expected} 个工具」`,
        loaded.summary === `当前：${PRESET_NAMES[value]} · ${expected} 个工具`, loaded.summary);
      if (value === 'readonly') {
        const tools = await page.evaluate(() => [...document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked')]
          .map((cb) => cb.value).sort());
        // 只读边界以 ToolSpec.side_effect 为准（config.py:1253-1261）：read_pdf / probe_video
        // 纯读取；拆书匠三件（novel_outline / novel_chunks / novel_split_text）只读文本、只回
        // JSON，也在档；会写产物的 pdf_render_pages / pdf_zoom_region / extract_frames 不进。
        check('只读模式只含 9 个只读工具（纯读取的 read_pdf / probe_video 与拆书匠三件在，会写产物三件不在）',
          JSON.stringify(tools) === JSON.stringify([
            'list_directory', 'novel_chunks', 'novel_outline', 'novel_split_text',
            'probe_video', 'read_file', 'read_pdf', 'search_files', 'vision_analyze',
          ]), JSON.stringify(tools));
      }
    }

    // ②.7 「添加自定义工具集」以**当前勾选**为底稿、且从不需要确认（「添加」是另存不是套用）。
    // 先制造一个真正的「未保存自定义组合」：载入只读模式后取消勾 1 个 → 9-1=8 个（≠ 任何预设个数
    // {9, 21, 25, 23, 37}），这样「添加不弹 confirm」才是有区分度的断言。
    await page.selectOption('#agentToolPresetCards', 'preset:readonly');
    await page.waitForTimeout(500);
    await page.click('.agent-tool-group[data-group="读取与检索"] .agent-tool-group-head');
    await page.waitForTimeout(250);
    await page.evaluate(() => {
      const cb = document.querySelector('.agent-tool-group[data-group="读取与检索"] input[type="checkbox"][value="read_pdf"]');
      if (cb && cb.checked) cb.click();
    });
    await page.waitForTimeout(300);
    const customBase = await page.evaluate(() => ({
      summary: document.querySelector('#agentToolPresetState')?.textContent.trim() || '',
      checked: document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked').length,
      tools: [...document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked')]
        .map((cb) => cb.value).sort(),
    }));
    check('前提：取消 1 个后变成「自定义（未保存）· 8 个工具」',
      customBase.summary === '当前：自定义（未保存）· 8 个工具' && customBase.checked === 8
      && customBase.tools.length === 8, JSON.stringify(customBase));
    dialogs.length = 0;
    await page.click('[data-tool-preset-add]');
    await page.waitForTimeout(400);
    const editorState = await page.evaluate(() => ({
      hasName: Boolean(document.querySelector('#agentToolSetName')),
      name: document.querySelector('#agentToolSetName')?.value || '',
      checked: document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked').length,
      tools: [...document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked')]
        .map((cb) => cb.value).sort(),
      delHidden: document.querySelector('#deleteAgentToolTemplate')?.hidden !== false,
      dialogH: Math.round(document.querySelector('#agentDialog')?.getBoundingClientRect().height || 0),
    }));
    check('「添加自定义工具集」在自定义未保存时也不弹确认（另存不是套用）', dialogs.length === 0, JSON.stringify(dialogs));
    check('「添加」以当前勾选为底稿（刚改出的 8 个，不是任何预设）',
      editorState.name === '' && editorState.checked === customBase.checked
      && JSON.stringify(editorState.tools) === JSON.stringify(customBase.tools), JSON.stringify(editorState));
    check('「添加」不设编辑目标：「删除工具集」仍隐藏', editorState.delHidden === true, String(editorState.delHidden));
    check('「添加」后弹层高度仍不变', Math.abs(editorState.dialogH - dialogH) <= 2, String(editorState.dialogH));

    let snap = await scopeSnapshot(page);
    check('弹层里有工具搜索框', snap.hasFilter === true, '');
    check('渲染 7 个分类', snap.groupCount === 7, JSON.stringify(snap.groups.map((g) => g.name)));
    check('分类顺序与接口一致', JSON.stringify(snap.groups.map((g) => g.name)) === JSON.stringify(EXPECTED_GROUPS),
      JSON.stringify(snap.groups.map((g) => g.name)));
    check('分类名与徽标逐项匹配',
      snap.groups.every((g) => EXPECTED_BADGES[g.name] === g.badge),
      JSON.stringify(snap.groups.map((g) => [g.name, g.badge])));
    check('徽标与分类名同行（不额外占列）', snap.groups.every((g) => g.badgeInline),
      JSON.stringify(snap.groups.map((g) => [g.name, g.badgeInline])));
    check('每个分类都有说明小字', snap.groups.every((g) => g.desc), JSON.stringify(snap.groups.map((g) => [g.name, g.desc])));
    check('分类头仍是一行（高度 ≤ 44px）', snap.groups.every((g) => g.headHeight <= 44),
      JSON.stringify(snap.groups.map((g) => [g.name, g.headHeight])));
    check('默认全部折叠（展开区隐藏）', snap.groups.every((g) => g.collapsed && g.bodyHidden), '');

    // ③ 展开「命令与脚本执行」→ 全选 → 计数与勾选状态
    const targetGroup = '命令与脚本执行';
    await page.click(`.agent-tool-group[data-group="${targetGroup}"] .agent-tool-group-head`);
    await page.waitForTimeout(300);
    snap = await scopeSnapshot(page);
    const expanded = snap.groups.find((g) => g.name === targetGroup);
    check(`展开「${targetGroup}」后工具可见`, expanded && !expanded.collapsed && !expanded.bodyHidden
      && expanded.toolCards === 2, JSON.stringify(expanded));
    const allCb = `.agent-tool-group[data-group="${targetGroup}"] input.group-select-all`;
    await page.evaluate((sel) => {
      const cb = document.querySelector(sel);
      if (cb && !cb.checked) cb.click();
    }, allCb);
    await page.waitForTimeout(300);
    const afterSelect = await page.evaluate((group) => {
      const el = document.querySelector(`.agent-tool-group[data-group="${group}"]`);
      const boxes = [...el.querySelectorAll('.permission-grid input[type="checkbox"]')];
      return {
        count: el.querySelector('.group-count')?.textContent.trim() || '',
        checked: boxes.filter((cb) => cb.checked).length,
        total: boxes.length,
      };
    }, targetGroup);
    check(`分类级全选把「${targetGroup}」全部勾上`, afterSelect.checked === afterSelect.total && afterSelect.total > 0,
      JSON.stringify(afterSelect));
    check('分类计数反映已选/总数', afterSelect.count === `${afterSelect.total}/${afterSelect.total}`,
      JSON.stringify(afterSelect));

    // ④ 搜索框：过滤成平铺结果 → 卡片带所属分类标签 → 清空恢复分组
    await page.fill('#agentToolFilter', 'vision');
    await page.waitForTimeout(400);
    const filtered = await page.evaluate(() => {
      const scope = document.querySelector('#agentToolScope');
      const boxes = [...scope.querySelectorAll('.permission-grid input[type="checkbox"]')];
      return {
        groupBlocks: scope.querySelectorAll('.agent-tool-group').length,
        names: boxes.map((cb) => cb.value),
        tags: [...scope.querySelectorAll('.tool-group-tag')].map((el) => el.textContent.trim()),
        headTitle: scope.querySelector('.group-title')?.childNodes[0]?.textContent.trim() || '',
      };
    });
    check('搜索命中 vision 相关工具', filtered.names.includes('vision_analyze') && filtered.names.includes('vision_image_ops'),
      JSON.stringify(filtered.names));
    check('搜索结果平铺成一个块', filtered.groupBlocks === 1 && filtered.headTitle === '搜索结果',
      JSON.stringify({ blocks: filtered.groupBlocks, head: filtered.headTitle }));
    check('搜索结果的卡片标出所属分类',
      filtered.tags.length === filtered.names.length && filtered.tags.every((tag) => tag === '视觉与图片'),
      JSON.stringify(filtered.tags));
    await page.fill('#agentToolFilter', '不存在的工具xyz');
    await page.waitForTimeout(300);
    const emptyHint = await page.evaluate(() => document.querySelector('#agentToolScope .tool-scope-empty')?.textContent.trim() || '');
    check('无匹配时给出空态提示', emptyHint.includes('没有匹配'), emptyHint);
    await page.fill('#agentToolFilter', '');
    await page.waitForTimeout(400);
    snap = await scopeSnapshot(page);
    check('清空搜索框恢复 7 组视图', snap.groupCount === 7, JSON.stringify(snap.groups.map((g) => g.name)));

    // ⑥ MCP 二级分组：拦截 /api/tool_catalog 注入假 MCP 工具（源码 server 未配 MCP 服务），
    //    验证「联网与外部服务」内按服务器分块 + 二级全选 + 计数。
    await page.route('**/api/tool_catalog', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      const injected = [
        { name: 'mcp__comfy-mcp__system_stats', description: '查看 ComfyUI 状态' },
        { name: 'mcp__comfy-mcp__run_workflow', description: '运行工作流' },
        { name: 'mcp__other__ping', description: '探活' },
      ];
      for (const tool of injected) {
        payload.tools.push({ ...tool, group: '联网与外部服务', subgroup: tool.name.split('__')[1], default_selected: false });
      }
      const net = (payload.groups || []).find((group) => group.name === '联网与外部服务');
      net.tools.push(...injected.map((tool) => tool.name));
      net.subgroups.push(
        { name: 'comfy-mcp', tools: ['mcp__comfy-mcp__system_stats', 'mcp__comfy-mcp__run_workflow'] },
        { name: 'other', tools: ['mcp__other__ping'] },
      );
      await route.fulfill({ json: payload });
    });
    await page.reload({ waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(800);
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForTimeout(400);
    await openAgentForm(null);
    await page.click('.agent-tool-group[data-group="联网与外部服务"] .agent-tool-group-head');
    await page.waitForTimeout(300);
    const subgroupSnap = await page.evaluate(() => {
      const group = document.querySelector('.agent-tool-group[data-group="联网与外部服务"]');
      const blocks = [...group.querySelectorAll('.tool-subgroup')];
      return {
        count: blocks.length,
        titles: blocks.map((block) => block.querySelector('.subgroup-title')?.textContent.trim() || ''),
        tools: blocks.map((block) => [...block.querySelectorAll('.permission-grid input[type="checkbox"]')].map((cb) => cb.value)),
        hasSelectAll: blocks.every((block) => Boolean(block.querySelector('input.subgroup-select-all'))),
        directTools: [...group.querySelectorAll('.agent-tool-group-body > .permission-grid input[type="checkbox"]')]
          .map((cb) => cb.value),
        groupCount: group.querySelector('.group-count')?.textContent.trim() || '',
      };
    });
    check('MCP 工具按服务器分成二级分组', subgroupSnap.count === 2 && JSON.stringify(subgroupSnap.titles) === JSON.stringify(['comfy-mcp', 'other']),
      JSON.stringify(subgroupSnap));
    check('二级分组内只放该服务器的工具',
      JSON.stringify(subgroupSnap.tools) === JSON.stringify([
        ['mcp__comfy-mcp__system_stats', 'mcp__comfy-mcp__run_workflow'], ['mcp__other__ping'],
      ]), JSON.stringify(subgroupSnap.tools));
    check('平铺区不重复出现二级分组里的工具',
      !subgroupSnap.directTools.some((name) => name.startsWith('mcp__')), JSON.stringify(subgroupSnap.directTools));
    check('每个二级分组都有全选框', subgroupSnap.hasSelectAll === true, JSON.stringify(subgroupSnap));
    await page.evaluate(() => {
      const cb = document.querySelector('.tool-subgroup input.subgroup-select-all');
      if (cb && !cb.checked) cb.click();
    });
    await page.waitForTimeout(300);
    const subgroupSelected = await page.evaluate(() => {
      const block = document.querySelector('.tool-subgroup');
      const boxes = [...block.querySelectorAll('.permission-grid input[type="checkbox"]')];
      return {
        checked: boxes.filter((cb) => cb.checked).length,
        total: boxes.length,
        count: block.querySelector('.subgroup-count')?.textContent.trim() || '',
      };
    });
    check('二级全选只勾选该服务器的工具',
      subgroupSelected.checked === subgroupSelected.total && subgroupSelected.total === 2
      && subgroupSelected.count === '2/2', JSON.stringify(subgroupSelected));

    // ⑧ 保存「我的工具集」→ 下拉的「我的工具集」分组出现该 option，可套用 / 删除。
    // 注意：工具集存后端（config.json 的 tool_sets），不是 localStorage——冻结版 pywebview
    // 默认 private_mode=True 会清空 localStorage，用户保存的工具集曾因此凭空消失。
    const beforeSave = await page.evaluate(() => [...document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked')]
      .map((cb) => cb.value).sort());
    await page.fill('#agentToolSetName', '冒烟工具集');
    await page.click('#agentToolEditorSave');
    await page.waitForTimeout(800);
    const savedState = await page.evaluate(() => {
      const select = document.querySelector('#agentToolPresetCards');
      const opt = select.querySelector('option[data-tool-template-card]');
      const mine = [...select.querySelectorAll('optgroup')].find((el) => el.label === '我的工具集');
      return {
        hasOption: Boolean(opt),
        optionText: opt?.textContent.trim() || '',
        optionTitle: opt?.getAttribute('title') || '',
        inMineGroup: Boolean(mine && opt && mine.contains(opt)),
        nameCleared: (document.querySelector('#agentToolSetName')?.value || '') === '',
        delHidden: document.querySelector('#deleteAgentToolTemplate')?.hidden !== false,
        viewVisible: document.querySelector('#agentToolPresetView')?.hidden === false,
        editorVisible: document.querySelector('#agentToolEditor')?.hidden === false,
        peekHidden: document.querySelector('#agentToolPeekList')?.hidden !== false,
        dialogH: Math.round(document.querySelector('#agentDialog')?.getBoundingClientRect().height || 0),
        legacyStored: localStorage.getItem('naiba.agentToolTemplates') || '',
      };
    });
    const storedSets = (await (await fetch(`${BASE}/api/tool_sets`)).json()).tool_sets || [];
    check('保存后「我的工具集」optgroup 出现该 option（新 DOM：下拉而非卡片）',
      savedState.hasOption && savedState.inMineGroup && storedSets.length === 1,
      JSON.stringify({ savedState, storedSets }));
    check('工具集落在后端 config.json（不再写 localStorage）',
      savedState.legacyStored === '' && storedSets[0]?.name === '冒烟工具集'
      && JSON.stringify([...storedSets[0].tools].sort()) === JSON.stringify(beforeSave),
      JSON.stringify({ legacyStored: savedState.legacyStored, stored: storedSets[0] }));
    check('option 文本 = 名称 + 工具个数（与保存的勾选一致）',
      savedState.optionText === `冒烟工具集 · ${beforeSave.length} 个工具`
      && savedState.optionTitle.split('、').filter(Boolean).length === beforeSave.length
      && savedState.optionTitle !== '我的工具集',
      JSON.stringify({ text: savedState.optionText, title: savedState.optionTitle }));
    check('保存后命名栏清空、下拉区与清单仍同屏、清单回默认收起',
      savedState.nameCleared && savedState.viewVisible && savedState.editorVisible && savedState.peekHidden,
      JSON.stringify(savedState));
    check('保存后编辑目标复位：「删除工具集」重新隐藏', savedState.delHidden === true, String(savedState.delHidden));
    check('保存后弹层高度仍不变', Math.abs(savedState.dialogH - dialogH) <= 2, String(savedState.dialogH));

    // 从下拉套用刚保存的这套：命名栏与勾选都回来；编辑目标就位 →「删除工具集」出现。
    const savedSetId = storedSets[0]?.id || '';
    dialogs.length = 0;
    await page.selectOption('#agentToolPresetCards', `template:${savedSetId}`);
    await page.waitForTimeout(600);
    const reopened = await page.evaluate(() => ({
      name: document.querySelector('#agentToolSetName')?.value || '',
      tools: [...document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked')]
        .map((cb) => cb.value).sort(),
      delVisible: document.querySelector('#deleteAgentToolTemplate')?.hidden === false,
      selectValue: document.querySelector('#agentToolPresetCards')?.value ?? '',
      dialogH: Math.round(document.querySelector('#agentDialog')?.getBoundingClientRect().height || 0),
    }));
    check('套用刚保存的这套不弹确认（勾选本就与它全等，不算覆盖）', dialogs.length === 0, JSON.stringify(dialogs));
    check('套用后命名栏与勾选都还原',
      reopened.name === '冒烟工具集' && JSON.stringify(reopened.tools) === JSON.stringify(beforeSave),
      JSON.stringify({ name: reopened.name, saved: beforeSave.length, opened: reopened.tools.length }));
    check('载入「我的工具集」后「删除工具集」按钮出现（删除入口只在编辑目标就位时给）',
      reopened.delVisible === true, String(reopened.delVisible));
    check('套用后下拉复位占位', reopened.selectValue === '', reopened.selectValue);
    check('套用后弹层高度仍不变', Math.abs(reopened.dialogH - dialogH) <= 2, String(reopened.dialogH));

    // 删除：独立按钮走「编辑中的那套」，confirm 一句话（还没有 Agent 引用它）。
    dialogs.length = 0;
    await page.click('#deleteAgentToolTemplate');
    await page.waitForTimeout(800);
    const leftSetsAfterDelete = (await (await fetch(`${BASE}/api/tool_sets`)).json()).tool_sets || [];
    check('删除是一句话确认（没有 Agent 在用，不带名单）',
      dialogs.length === 1 && (dialogs[0] || '') === '确定删除工具集「冒烟工具集」吗？', dialogs[0] || '');
    check('确认后后端记录清掉、下拉里 option 消失',
      leftSetsAfterDelete.length === 0
      && await page.evaluate(() => document.querySelectorAll('#agentToolPresetCards option[data-tool-template-card]').length === 0),
      JSON.stringify(leftSetsAfterDelete));

    // ⑨ MCP 工具「消失」回归（用户实测 bug）：MCP 是按需连接的，应用刚启动时拉到的目录里
    //    没有 mcp__* 工具；若永久缓存这份目录，整个页面会话的工具集面板就再也看不到 MCP 工具。
    //    这里让第一次目录响应缺少 MCP 工具，再打开工具集面板——必须强制取新、把工具带回来。
    await page.unroute('**/api/tool_catalog');
    const lateMcp = [
      { name: 'mcp__smoke-mcp__ping', description: '探活' },
      { name: 'mcp__smoke-mcp__stats', description: '看状态' },
    ];
    let servedFirst = false;
    await page.route('**/api/tool_catalog', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      if (!servedFirst) {
        servedFirst = true;
        await route.fulfill({ json: payload });   // 第一次 = 启动时那份（还没有 MCP 工具）
        return;
      }
      for (const tool of lateMcp) {
        payload.tools.push({ ...tool, group: '联网与外部服务', subgroup: 'smoke-mcp', default_selected: false });
      }
      const net = (payload.groups || []).find((group) => group.name === '联网与外部服务');
      net.tools.push(...lateMcp.map((tool) => tool.name));
      net.subgroups.push({ name: 'smoke-mcp', tools: lateMcp.map((tool) => tool.name) });
      await route.fulfill({ json: payload });
    });
    await page.reload({ waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(900);
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForTimeout(500);
    await openAgentForm(null);
    await page.click('.agent-tool-group[data-group="联网与外部服务"] .agent-tool-group-head');
    await page.waitForTimeout(300);
    const lateMcpSnap = await page.evaluate(() => ({
      mcpBoxes: [...document.querySelectorAll('#agentToolScope input[type="checkbox"]')]
        .filter((cb) => cb.value.startsWith('mcp__')).map((cb) => cb.value),
      hasSubgroup: Boolean(document.querySelector('.agent-tool-group[data-group="联网与外部服务"] .tool-subgroup')),
    }));
    check('启动目录缺 MCP 工具时，打开面板会强制取新（MCP 工具回来了）',
      lateMcpSnap.mcpBoxes.length === 2 && lateMcpSnap.hasSubgroup, JSON.stringify(lateMcpSnap));
    await page.unroute('**/api/tool_catalog');

    // ⑩ 自定义组合的保底 / 点下拉误触覆盖 / 删除工具集看不见「谁在用」（三处一起回归）。
    //    播种：工具集 S（被 A1 引用）、没人用的 S2；Agent A1（tool_scope = S 的工具）、
    //    A2（一套匹配不上任何模板的自定义组合）、A3（未限制，套用无损）。
    //    数量刻意挑成 ≠ 任何预设的个数，否则「自定义未保存」这个前提就不成立。
    //    池子排除 mcp__ 与 opt-in（extract_video_frames_exact 单独授权，前端打开表单时会把它
    //    从 tool_scope 摘出去，混进播种集合会让计数断言失真），再排序取前缀。
    const catalogNow = await (await fetch(`${BASE}/api/tool_catalog`)).json();
    const totalCatalogTools = (catalogNow.tools || []).length;
    const optInCount = (catalogNow.tools || []).filter((tool) => tool.opt_in).length;
    const denom = totalCatalogTools - optInCount;   // 与 updateToolCounter 的分母同口径
    const pool = (catalogNow.tools || [])
      .filter((tool) => !tool.name.startsWith('mcp__') && !tool.opt_in)
      .map((tool) => tool.name).sort();
    const seedTools = pool.slice(0, 24);       // 24 个：不与任何预设个数相同（且天然满足依赖闭包）
    // 22 个：预设个数现在是 {readonly 9, standard 21, longsession 25, comfyui 23, full 37}
    //（config.py TOOL_PRESETS，见文件头②段的逐档对账），旧值 23 已与 comfyui 相撞 —— 挑 22；
    // slice 前缀天然带 comfyui_batch 的依赖闭包（job_output/job_status/job_wait 都在前 22），
    // normalizeToolScope 不会再加数，A2 打开表单后的勾选仍是 22。
    const customTools = pool.slice(0, 22);
    const presetCounts = (catalogNow.presets || []).map((preset) => (preset.tools || []).length);
    check('前提：播种的 24 / 22 个工具都不等于任何预设的个数（否则不是「自定义未保存」）',
      !presetCounts.includes(seedTools.length) && !presetCounts.includes(customTools.length),
      JSON.stringify(presetCounts));
    const postJson = async (path, body) => (await fetch(`${BASE}${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })).json();
    // POST /api/tool_sets 的回包是 { tool_set: {...} }（不是裸对象），id 要往里取一层。
    const setS = (await postJson('/api/tool_sets', { name: '冒烟-被引用工具集', tools: seedTools })).tool_set;
    const setS2 = (await postJson('/api/tool_sets', { name: '冒烟-没人用工具集', tools: pool.slice(0, 3) })).tool_set;
    const a1 = await postJson('/api/agents', {
      name: '冒烟-A1引用工具集', system_prompt: '', skill_ids: [], tool_scope: seedTools,
    });
    const a2 = await postJson('/api/agents', {
      name: '冒烟-A2自定义', system_prompt: '', skill_ids: [], tool_scope: customTools,
    });
    const a3 = await postJson('/api/agents', {
      name: '冒烟-A3未限制', system_prompt: '', skill_ids: [], tool_scope: [],
    });
    seedAgents = [a1.id, a2.id, a3.id].filter(Boolean);
    seedSets = [setS.id, setS2.id].filter(Boolean);
    check('播种成功（3 个 Agent + 2 套工具集）', seedAgents.length === 3 && seedSets.length === 2,
      JSON.stringify({ seedAgents, seedSets }));

    await page.reload({ waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(800);
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForTimeout(600);
    const labels = await page.evaluate((ids) => {
      const out = {};
      for (const id of ids) {
        out[id] = document.querySelector(`[data-agent-card="${id}"] .agent-card-tools`)?.textContent.trim() || '';
      }
      return out;
    }, [a1.id, a2.id, a3.id]);
    check('Agent 卡片的工具集标签：引用命中显示工具集名，其余显示「自定义/未限制」',
      labels[a1.id] === '工具集：冒烟-被引用工具集'
      && labels[a2.id] === `工具集：自定义 · ${customTools.length} 个工具`
      && labels[a3.id] === '工具集：未限制（全部工具）', JSON.stringify(labels));

    // A2：自定义未保存 → 同屏的清单已经带着当前勾选，直接可改（不再需要「编辑当前工具集」入口）。
    await openAgentForm(a2.id);
    const a2State = await page.evaluate(() => ({
      summary: document.querySelector('#agentToolPresetState')?.textContent.trim() || '',
      tabCount: document.querySelector('#agentToolTabCount')?.textContent.trim() || '',
      editorVisible: document.querySelector('#agentToolEditor')?.hidden === false,
      viewVisible: document.querySelector('#agentToolPresetView')?.hidden === false,
      name: document.querySelector('#agentToolSetName')?.value || '',
      delHidden: document.querySelector('#deleteAgentToolTemplate')?.hidden !== false,
      checked: document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked').length,
      tools: [...document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked')]
        .map((cb) => cb.value).sort(),
    }));
    check('前提：A2 的摘要就是「自定义（未保存）」',
      a2State.summary === `当前：自定义（未保存）· ${customTools.length} 个工具`
      && a2State.tabCount === `${customTools.length}/${denom}`, JSON.stringify(a2State));
    check('打开即同屏：下拉区与勾选清单同时可见', a2State.editorVisible && a2State.viewVisible, JSON.stringify(a2State));
    check(`同屏后无需任何入口：清单已带着 A2 的 ${customTools.length} 个勾选，一个不换（核心断言）`,
      a2State.checked === customTools.length && JSON.stringify(a2State.tools) === JSON.stringify(customTools),
      JSON.stringify(a2State));
    check('自定义组合没有名字：命名栏留空（保存时另存一套新的工具集）', a2State.name === '', a2State.name);
    check('未载入模板：「删除工具集」隐藏（不会误删别的模板）', a2State.delHidden === true, String(a2State.delHidden));

    // 防误触：自定义未保存时选模板先 confirm；取消 = 原状不动（同屏下没有「卡片态」可回，验证的是勾选分毫未改）。
    dialogs.length = 0;
    dialogHandler = (dialog) => dialog.dismiss();
    await page.selectOption('#agentToolPresetCards', 'preset:standard');
    await page.waitForTimeout(500);
    const afterDismiss = await stageVisibility();
    const dismissScope = await page.evaluate(() => ({
      summary: document.querySelector('#agentToolPresetState')?.textContent.trim() || '',
      checked: document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked').length,
      name: document.querySelector('#agentToolSetName')?.value || '',
    }));
    check('自定义未保存时选模板先弹确认（只弹一次）', dialogs.length === 1, JSON.stringify(dialogs));
    check('确认文案说清「会替换现在勾选的全部工具」',
      (dialogs[0] || '').includes('会替换现在勾选的全部工具'), dialogs[0] || '');
    check('取消后留在原状：勾选分毫不动、下拉复位占位（核心断言）',
      afterDismiss.editorVisible && afterDismiss.viewVisible && afterDismiss.selectValue === ''
      && dismissScope.summary === `当前：自定义（未保存）· ${customTools.length} 个工具`
      && dismissScope.checked === customTools.length && dismissScope.name === '',
      JSON.stringify({ afterDismiss, dismissScope }));

    dialogs.length = 0;
    dialogHandler = (dialog) => dialog.accept();
    await page.selectOption('#agentToolPresetCards', 'preset:standard');
    await page.waitForTimeout(700);
    const afterAccept = await page.evaluate(() => ({
      editorVisible: document.querySelector('#agentToolEditor')?.hidden === false,
      checked: document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked').length,
      name: document.querySelector('#agentToolSetName')?.value || '',
      selectValue: document.querySelector('#agentToolPresetCards')?.value ?? '',
    }));
    check('确认后整份替换为该预设（标准模式 21 个）且命名栏预填',
      afterAccept.editorVisible && afterAccept.checked === 21 && afterAccept.name === '标准模式'
      && afterAccept.selectValue === '', JSON.stringify(afterAccept));
    await closeAgentForm();

    // 反例：未限制的 Agent 选模板无损，不该弹确认。
    await openAgentForm(a3.id);
    const a3Summary = await page.evaluate(() => document.querySelector('#agentToolPresetState')?.textContent.trim() || '');
    check('前提：A3 是未限制', a3Summary.includes('未限制'), a3Summary);
    dialogs.length = 0;
    await page.selectOption('#agentToolPresetCards', 'preset:readonly');
    await page.waitForTimeout(700);
    const a3Opened = await page.evaluate(() => ({
      editorVisible: document.querySelector('#agentToolEditor')?.hidden === false,
      checked: document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked').length,
    }));
    check('未限制的 Agent 选模板不弹确认，直接套用（无损）',
      dialogs.length === 0 && a3Opened.editorVisible && a3Opened.checked === 9,
      JSON.stringify({ dialogs, a3Opened }));

    // 删除工具集：被引用时列出「谁在用」并说明拷贝语义；没人用时仍是一句话确认。
    // 新 DOM 里删除按钮删的是「刚载入的那套」（agentToolEditingId），所以先从下拉载入、再点删除。
    dialogs.length = 0;
    dialogHandler = (dialog) => dialog.dismiss();
    await page.selectOption('#agentToolPresetCards', `template:${setS.id}`);
    await page.waitForTimeout(600);
    const delBtnReady = await page.evaluate(() => document.querySelector('#deleteAgentToolTemplate')?.hidden === false);
    check('载入被引用的工具集后「删除工具集」按钮就位', delBtnReady === true, String(delBtnReady));
    dialogs.length = 0;
    await page.click('#deleteAgentToolTemplate');
    await page.waitForTimeout(800);
    const setsAfterDismiss = (await (await fetch(`${BASE}/api/tool_sets`)).json()).tool_sets || [];
    check('删除被引用的工具集：confirm 里列出在用的 Agent 名',
      dialogs.length === 1 && (dialogs[0] || '').includes('冒烟-A1引用工具集'), dialogs[0] || '');
    check('删除提示说明「不会改动这些 Agent 已保存的配置」（拷贝语义）',
      (dialogs[0] || '').includes('不会改动'), dialogs[0] || '');
    check('取消删除：后端记录还在', setsAfterDismiss.some((item) => item.id === setS.id),
      JSON.stringify(setsAfterDismiss.map((item) => item.name)));

    dialogHandler = (dialog) => dialog.accept();
    await page.selectOption('#agentToolPresetCards', `template:${setS2.id}`);
    await page.waitForTimeout(600);
    dialogs.length = 0;
    await page.click('#deleteAgentToolTemplate');
    await page.waitForTimeout(900);
    const setsAfterDelete = (await (await fetch(`${BASE}/api/tool_sets`)).json()).tool_sets || [];
    check('没人用的工具集：仍是一句话确认（不带 Agent 名单）',
      (dialogs[0] || '') === '确定删除工具集「冒烟-没人用工具集」吗？', dialogs[0] || '');
    check('确认后删掉该工具集，被引用的那套不受影响',
      !setsAfterDelete.some((item) => item.id === setS2.id) && setsAfterDelete.some((item) => item.id === setS.id),
      JSON.stringify(setsAfterDelete.map((item) => item.name)));
    await closeAgentForm();
    dialogHandler = (dialog) => dialog.accept();

    // ⑦ 零页面错误（已知噪音 /api/providers/models 不计，其余 4xx/5xx 与 console.error 都算）
    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
    check('零非预期 4xx/5xx 响应', badResponses.length === 0, JSON.stringify(badResponses.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    // 播种的 Agent / 工具集一律回收，别留在隔离数据目录里（异常路径也要收）。
    // 只认「冒烟-」前缀：万一 id 没拿到（接口回包结构变了），按名字兜底也能清干净。
    try {
      const leftAgents = (await (await fetch(`${BASE}/api/agents`)).json()).agents || [];
      for (const agent of leftAgents) {
        if (String(agent.name || '').startsWith('冒烟-')) {
          await fetch(`${BASE}/api/agents/${encodeURIComponent(agent.id)}`, { method: 'DELETE' });
        }
      }
      const leftSets = (await (await fetch(`${BASE}/api/tool_sets`)).json()).tool_sets || [];
      for (const item of leftSets) {
        if (String(item.name || '').startsWith('冒烟-')) {
          await fetch(`${BASE}/api/tool_sets/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
        }
      }
      const afterAgents = (await (await fetch(`${BASE}/api/agents`)).json()).agents || [];
      const afterSets = (await (await fetch(`${BASE}/api/tool_sets`)).json()).tool_sets || [];
      check('播种数据已回收干净（不留冒烟 Agent / 工具集）',
        !afterAgents.some((item) => String(item.name || '').startsWith('冒烟-'))
        && !afterSets.some((item) => String(item.name || '').startsWith('冒烟-')),
        JSON.stringify({ agents: afterAgents.map((item) => item.name), sets: afterSets.map((item) => item.name) }));
    } catch (error) {
      check('播种数据回收核对', false, String(error && error.message ? error.message : error));
    }
    await browser.close();
  }

  console.log(failures.length
    ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}`
    : `\nALL PASS（${passes} 项）`);
  process.exit(failures.length ? 1 : 0);
})();
