// 工具分类改版冒烟（随仓库发布的可复用资产；由 tool_groups_smoke.py 起源码 server 后调用）。
// 覆盖：卡片态（4 张只读预设 + 添加卡 + 弹层固定高度）/ 点卡片的「按下」反馈与进入编辑态 /
//       命名栏预填卡片名 / 编辑态与卡片态弹层同高 / 保存「我的工具集」与复用、× 删除 /
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
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('dialog', (dialog) => dialog.accept());

  try {
    // ① 接口数据：分组顺序 / 徽标 / 二级分组字段齐备
    const catalog = await (await fetch(`${BASE}/api/tool_catalog`)).json();
    const apiGroups = catalog.groups || [];
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
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForTimeout(400);
    await page.click('#agentCards .agent-card');
    await page.waitForTimeout(600);
    // 分区切换后默认停在「基本」，工具集相关断言要先切到「工具集」页。
    await page.click('[data-agent-tab="tools"]');
    await page.waitForTimeout(300);

    // ②.5 卡片态：5 张内置预设 + 1 张「添加自定义工具集」卡；工具列表此时不展开。
    const cardState = await page.evaluate(() => ({
      cards: [...document.querySelectorAll('#agentToolPresetCards .tool-preset-card')].map((el) => ({
        preset: el.dataset.toolPresetCard || '',
        add: el.dataset.toolPresetAdd !== undefined,
        title: el.querySelector('b')?.textContent.trim() || '',
        count: el.querySelector('em')?.textContent.trim() || '',
      })),
      editorHidden: document.querySelector('#agentToolEditor')?.hidden === true,
      summary: document.querySelector('#agentToolPresetState')?.textContent.trim() || '',
      dialogH: Math.round(document.querySelector('#agentDialog')?.getBoundingClientRect().height || 0),
    }));
    check('卡片态显示 5 张预设 + 1 张添加卡',
      cardState.cards.filter((c) => c.preset).length === 5 && cardState.cards.some((c) => c.add),
      JSON.stringify(cardState.cards));
    check('预设卡名称与个数正确', JSON.stringify(cardState.cards.filter((c) => c.preset).map((c) => [c.title, c.count]))
      === JSON.stringify([
        ['只读模式', '6 个工具'], ['标准模式', '13 个工具'], ['长会话模式', '17 个工具'],
        ['ComfyUI 联动', '18 个工具'],
        ['全能模式', `${(catalog.tools || []).length} 个工具`],
      ]), JSON.stringify(cardState.cards.map((c) => [c.title, c.count])));
    check('卡片态不展开工具列表', cardState.editorHidden === true, JSON.stringify(cardState));
    check('卡片态与编辑态弹层同高（固定，不跳）', cardState.dialogH > 600, String(cardState.dialogH));
    const dialogH = cardState.dialogH;

    // 点预设卡：不直接套用，而是「按下 → 卡片上移收起 → 列表从下方滑入」，命名栏预填该卡名字。
    const PRESET_NAMES = { readonly: '只读模式', standard: '标准模式', comfyui: 'ComfyUI 联动', full: '全能模式' };
    for (const [value, expected] of [['readonly', 6], ['standard', 13], ['comfyui', 18],
      ['full', (catalog.tools || []).length]]) {
      await page.click(`[data-tool-preset-card="${value}"]`);
      // 先量「按下」那一帧：被点的卡片必须立刻带 .is-picked（选中反馈）。
      const picked = await page.evaluate((sel) => {
        const card = document.querySelector(sel);
        return {
          picked: card.classList.contains('is-picked'),
          pressed: card.getAttribute('aria-pressed') || '',
        };
      }, `[data-tool-preset-card="${value}"]`);
      check(`点「${value}」卡立刻有按下反馈`, picked.picked === true && picked.pressed === 'true',
        JSON.stringify(picked));
      await page.waitForTimeout(600);
      const opened = await page.evaluate(() => ({
        editorVisible: document.querySelector('#agentToolEditor')?.hidden === false,
        cardsHidden: document.querySelector('#agentToolPresetView')?.hidden === true,
        name: document.querySelector('#agentToolSetName')?.value || '',
        checked: document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked').length,
        dialogH: Math.round(document.querySelector('#agentDialog')?.getBoundingClientRect().height || 0),
      }));
      check(`点「${value}」卡进入编辑态（卡片收起 + 列表展开）`,
        opened.editorVisible && opened.cardsHidden, JSON.stringify(opened));
      check(`命名栏自动预填「${PRESET_NAMES[value]}」`, opened.name === PRESET_NAMES[value], JSON.stringify(opened));
      check(`载入「${value}」的工具个数 == 卡片显示个数（${expected}）`,
        opened.checked === expected, JSON.stringify(opened));
      check(`编辑态弹层高度不变（${dialogH}px）`, Math.abs(opened.dialogH - dialogH) <= 2, String(opened.dialogH));
      if (value === 'readonly') {
        const tools = await page.evaluate(() => [...document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked')]
          .map((cb) => cb.value).sort());
        check('只读模式只含 6 个只读工具（纯读取的 read_pdf / probe_video 在，会写产物三件不在）',
          JSON.stringify(tools) === JSON.stringify([
            'list_directory', 'probe_video', 'read_file', 'read_pdf', 'search_files',
            'vision_analyze',
          ]), JSON.stringify(tools));
      }
      await page.click('#agentToolEditorBack');
      await page.waitForTimeout(600);
      const back = await page.evaluate(() => ({
        cardsVisible: document.querySelector('#agentToolPresetView')?.hidden === false,
        editorHidden: document.querySelector('#agentToolEditor')?.hidden === true,
        active: document.querySelector('[data-tool-preset-card].is-active')?.dataset.toolPresetCard || '',
        summary: document.querySelector('#agentToolPresetState')?.textContent.trim() || '',
      }));
      check(`返回卡片态：选中「${value}」的卡高亮且摘要带名字`,
        back.cardsVisible && back.editorHidden && back.active === value
        && back.summary.includes(PRESET_NAMES[value]), JSON.stringify(back));
    }

    // 进入编辑态：点「添加自定义工具集」→ 卡片区收起、横条 + 工具列表展开、
    // 命名栏留空、工具以「标准模式」为起点（不跟随当前选中项）。
    await page.click('[data-tool-preset-add]');
    await page.waitForTimeout(120);
    const addPicked = await page.evaluate(() => document.querySelector('[data-tool-preset-add]')?.classList.contains('is-picked') === true);
    check('「添加自定义工具集」卡点击后也有按下反馈', addPicked === true, '');
    await page.waitForTimeout(400);
    const editorState = await page.evaluate(() => ({
      editorVisible: document.querySelector('#agentToolEditor')?.hidden === false,
      cardsHidden: document.querySelector('#agentToolPresetView')?.hidden === true,
      hasName: Boolean(document.querySelector('#agentToolSetName')),
      name: document.querySelector('#agentToolSetName')?.value || '',
      checked: document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked').length,
      tools: [...document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked')]
        .map((cb) => cb.value).sort(),
      dialogH: Math.round(document.querySelector('#agentDialog')?.getBoundingClientRect().height || 0),
    }));
    check('点添加卡进入编辑态（卡片收起 + 编辑区展开）',
      editorState.editorVisible && editorState.cardsHidden && editorState.hasName, JSON.stringify(editorState));
    check('「添加」卡命名栏留空', editorState.name === '', JSON.stringify(editorState));
    check('「添加」卡默认载入标准模式的 13 个工具（不跟随当前选中项）',
      editorState.checked === 13 && JSON.stringify(editorState.tools) === JSON.stringify([
        'edit_file', 'extract_frames', 'list_directory', 'pdf_render_pages', 'pdf_zoom_region',
        'probe_video', 'pwsh', 'read_file', 'read_pdf', 'run_skill_script',
        'search_files', 'vision_analyze', 'write_file',
      ]), JSON.stringify(editorState));
    check('编辑态弹层高度仍与卡片态一致', Math.abs(editorState.dialogH - dialogH) <= 2, String(editorState.dialogH));

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
    await page.click('#agentCards .agent-card');
    await page.waitForTimeout(600);
    await page.click('[data-agent-tab="tools"]');
    await page.waitForTimeout(300);
    await page.click('[data-tool-preset-add]');
    await page.waitForTimeout(500);
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

    // ⑧ 保存「我的工具集」→ 卡片出现并可套用/删除。
    // 注意：工具集存后端（config.json 的 tool_sets），不是 localStorage——冻结版 pywebview
    // 默认 private_mode=True 会清空 localStorage，用户保存的工具集曾因此凭空消失。
    const beforeSave = await page.evaluate(() => [...document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked')]
      .map((cb) => cb.value).sort());
    await page.fill('#agentToolSetName', '冒烟工具集');
    await page.click('#agentToolEditorSave');
    await page.waitForTimeout(800);
    const savedCard = await page.evaluate(() => {
      const card = document.querySelector('[data-tool-template-card]');
      return {
        hasCard: Boolean(card),
        name: card?.querySelector('b')?.textContent.trim() || '',
        count: card?.querySelector('em')?.textContent.trim() || '',
        hasDelete: Boolean(card?.querySelector('[data-tool-template-del]')),
        editorHidden: document.querySelector('#agentToolEditor')?.hidden === true,
        dialogH: Math.round(document.querySelector('#agentDialog')?.getBoundingClientRect().height || 0),
        legacyStored: localStorage.getItem('naiba.agentToolTemplates') || '',
      };
    });
    const storedSets = (await (await fetch(`${BASE}/api/tool_sets`)).json()).tool_sets || [];
    check('保存后回到卡片态并出现「我的工具集」卡片',
      savedCard.hasCard && savedCard.editorHidden && storedSets.length === 1, JSON.stringify({ savedCard, storedSets }));
    check('工具集落在后端 config.json（不再写 localStorage）',
      savedCard.legacyStored === '' && storedSets[0]?.name === '冒烟工具集'
      && JSON.stringify([...storedSets[0].tools].sort()) === JSON.stringify(beforeSave),
      JSON.stringify({ legacyStored: savedCard.legacyStored, stored: storedSets[0] }));
    check('卡片显示工具集名与个数', savedCard.name === '冒烟工具集' && /^\d+ 个工具$/.test(savedCard.count),
      JSON.stringify(savedCard));
    check('「我的工具集」卡片带 × 删除入口', savedCard.hasDelete === true, '');
    check('保存后弹层高度仍不变', Math.abs(savedCard.dialogH - dialogH) <= 2, String(savedCard.dialogH));

    // 点自定义卡：回到编辑态，名字与勾选都回来（不是直接套用）
    await page.click('[data-tool-template-card]');
    await page.waitForTimeout(600);
    const reopened = await page.evaluate(() => ({
      editorVisible: document.querySelector('#agentToolEditor')?.hidden === false,
      cardsHidden: document.querySelector('#agentToolPresetView')?.hidden === true,
      name: document.querySelector('#agentToolSetName')?.value || '',
      tools: [...document.querySelectorAll('#agentToolScope .permission-grid input[type="checkbox"]:checked')]
        .map((cb) => cb.value).sort(),
      dialogH: Math.round(document.querySelector('#agentDialog')?.getBoundingClientRect().height || 0),
    }));
    check('点「我的工具集」卡回到编辑态，命名栏与勾选都还原',
      reopened.editorVisible && reopened.cardsHidden && reopened.name === '冒烟工具集'
      && JSON.stringify(reopened.tools) === JSON.stringify(beforeSave),
      JSON.stringify({ name: reopened.name, saved: beforeSave.length, opened: reopened.tools.length }));
    check('自定义卡进入编辑态时弹层高度仍不变', Math.abs(reopened.dialogH - dialogH) <= 2, String(reopened.dialogH));

    await page.click('#agentToolEditorBack');
    await page.waitForTimeout(600);
    const backToCards = await page.evaluate(() => ({
      cardsVisible: document.querySelector('#agentToolPresetView')?.hidden === false,
      activePreset: document.querySelector('[data-tool-preset-card].is-active')?.dataset.toolPresetCard || '',
      activeTemplate: document.querySelector('[data-tool-template-card].is-active')?.querySelector('b')?.textContent.trim() || '',
      summary: document.querySelector('#agentToolPresetState')?.textContent.trim() || '',
    }));
    check('返回卡片态：自定义卡自己高亮、内置预设卡不高亮',
      backToCards.cardsVisible && backToCards.activePreset === '' && backToCards.activeTemplate === '冒烟工具集'
      && backToCards.summary.includes('冒烟工具集'), JSON.stringify(backToCards));

    await page.click('[data-tool-template-del]');
    await page.waitForTimeout(700);
    const afterDelete = await page.evaluate(() => ({
      cards: document.querySelectorAll('[data-tool-template-card]').length,
    }));
    const leftSets = (await (await fetch(`${BASE}/api/tool_sets`)).json()).tool_sets || [];
    check('× 删除（确认后）卡片与后端记录同步清掉',
      afterDelete.cards === 0 && leftSets.length === 0, JSON.stringify({ afterDelete, leftSets }));

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
    await page.click('#agentCards [data-agent-card] .agent-card-name');
    await page.waitForSelector('#agentDialog[open]', { timeout: 10000 });
    await page.waitForTimeout(800);
    await page.click('[data-agent-tab="tools"]');
    await page.waitForTimeout(400);
    await page.click('[data-tool-preset-add]');
    await page.waitForTimeout(900);
    const lateMcpSnap = await page.evaluate(() => ({
      mcpBoxes: [...document.querySelectorAll('#agentToolScope input[type="checkbox"]')]
        .filter((cb) => cb.value.startsWith('mcp__')).map((cb) => cb.value),
      hasSubgroup: Boolean(document.querySelector('.agent-tool-group[data-group="联网与外部服务"] .tool-subgroup')),
    }));
    check('启动目录缺 MCP 工具时，打开面板会强制取新（MCP 工具回来了）',
      lateMcpSnap.mcpBoxes.length === 2 && lateMcpSnap.hasSubgroup, JSON.stringify(lateMcpSnap));
    await page.unroute('**/api/tool_catalog');

    // ⑦ 零页面错误
    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length
    ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}`
    : `\nALL PASS（${passes} 项）`);
  process.exit(failures.length ? 1 : 0);
})();
