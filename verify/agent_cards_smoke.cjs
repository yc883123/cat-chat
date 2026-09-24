// Agent 卡片化冒烟（源码 server，端口 8790）。
// 覆盖：三列网格 / 末尾「新增 Agent」卡 / 点卡片弹出并加载该 Agent 预设 / 字段可编辑且 ID 锁定 /
//       Esc 只关最上层 / 新增卡片建号并出现 / × 删除走确认 / 窄屏 2 与 1 列 /
//       出厂内置 6 样齐全并置顶、带 emoji 头像、显示「内置」徽标且无删除按钮 /
//       遗留副本（dsh-standard, built_in=true）启动后摘标记、显示 × 且可删 / 零页面错误。
// 前置：先 `python verify/seed_legacy_builtin_agent.py`，再启动源码 server（config.json 事后还原）。
// 运行：$env:NODE_PATH="<node_modules 目录>"; node verify\agent_cards_smoke.cjs
// 也可对着**隔离实例**跑（推荐，不碰真 config/真数据目录）：先往隔离 root 的 config.json 里塞一条
//       dsh-standard（built_in=true），再 `NAIBA_TMP_PORT=8790 python verify\_serve_tmp.py`，
//       然后 `NAIBA_SMOKE_BASE=http://127.0.0.1:8790 node verify\agent_cards_smoke.cjs`。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const PREFIX = '冒烟Agent';
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

async function cleanupSeeded() {
  const bootstrap = await apiJson('/api/bootstrap');
  for (const agent of bootstrap.agents || []) {
    if (String(agent.name || '').startsWith(PREFIX) || String(agent.id || '').startsWith('smoke_agent')) {
      await apiJson(`/api/agents/${encodeURIComponent(agent.id)}`, { method: 'DELETE' });
    }
  }
}

async function cardSnapshot(page) {
  return page.evaluate(() => {
    const container = document.querySelector('#agentCards');
    if (!container) return null;
    const cards = [...container.querySelectorAll('.agent-card')];
    const add = container.querySelector('[data-agent-add]');
    return {
      total: cards.length,
      columns: getComputedStyle(container).gridTemplateColumns.split(' ').filter(Boolean).length,
      addIsLast: Boolean(add) && container.lastElementChild === add,
      addText: add ? add.textContent.trim() : '',
      agents: cards.filter((card) => card.dataset.agentCard).map((card) => ({
        id: card.dataset.agentCard,
        name: card.querySelector('.agent-card-name')?.textContent.trim() || '',
        meta: card.querySelector('.agent-card-meta')?.textContent.trim() || '',
        tools: card.querySelector('.agent-card-tools')?.textContent.trim() || '',
        prompt: card.querySelector('.agent-card-prompt')?.textContent.trim() || '',
        badges: [...card.querySelectorAll('.agent-card-badge, .agent-card-tag')].map((el) => el.textContent.trim()),
        hasDelete: Boolean(card.querySelector('[data-agent-delete]')),
        cursor: getComputedStyle(card).cursor,
        className: card.className,
        background: getComputedStyle(card).backgroundColor,
        borderColor: getComputedStyle(card).borderTopColor,
      })),
    };
  });
}

async function dialogSnapshot(page) {
  return page.evaluate(() => {
    const dialog = document.querySelector('#agentDialog');
    return {
      open: Boolean(dialog && dialog.open),
      title: document.querySelector('#agentDialogTitle')?.textContent.trim() || '',
      subtitle: document.querySelector('#agentDialogSubtitle')?.textContent.trim() || '',
      name: document.querySelector('#agentName')?.value || '',
      formId: document.querySelector('#agentFormId')?.value || '',
      hasIdField: Boolean(document.querySelector('#agentId')),
      prompt: document.querySelector('#agentSystemPromptEdit')?.value || '',
      hasSkillPicker: Boolean(document.querySelector('#agentSkillList')),
      hasToolScope: Boolean(document.querySelector('#agentToolScope')),
      legacyEditButton: Boolean(document.querySelector('[data-agent-edit]')),
      hasAvatarButton: Boolean(document.querySelector('#pickAgentAvatar')),
    };
  });
}

// 固定 Skill / 工具集必须真的可见可点（本轮修的回归：滚动容器被网格行裁掉）。
// 分区切换后两个面板不同时可见 → 各自切到对应页再量。
async function skillSnapshot(page) {
  await page.click('[data-agent-tab="skills"]');
  await page.waitForTimeout(250);
  return page.evaluate(() => {
    const skills = document.querySelector('#agentSkillList');
    const skillsBox = skills.closest('.agent-tab-panel').getBoundingClientRect();
    const cards = [...skills.querySelectorAll('.skill-card')];
    const cardStyle = cards.length ? getComputedStyle(cards[0]) : null;
    return {
      skillCards: cards.length,
      legacySkillItems: skills.querySelectorAll('.skill-item').length,
      skillCardBorder: cardStyle ? cardStyle.borderTopWidth : '',
      skillCardRadius: cardStyle ? cardStyle.borderTopLeftRadius : '',
      skillCardColumns: getComputedStyle(skills).gridTemplateColumns.split(' ').filter(Boolean).length,
      listH: Math.round(skills.getBoundingClientRect().height),
      boxH: Math.round(skillsBox.height),
    };
  });
}

async function toolSnapshot(page) {
  await page.click('[data-agent-tab="tools"]');
  await page.waitForTimeout(250);
  // 卡片态默认不展开工具列表：点「添加自定义工具集」卡进入编辑态后再量。
  if (await page.$('[data-tool-preset-add]')) {
    await page.click('[data-tool-preset-add]');
    await page.waitForTimeout(450);
  }
  return page.evaluate(() => {
    const scope = document.querySelector('#agentToolScope');
    const scopeBox = scope.closest('.agent-tab-panel').getBoundingClientRect();
    const groups = [...scope.querySelectorAll('.agent-tool-group')];
    const groupInfo = groups.map((group) => {
      const head = group.querySelector('.agent-tool-group-head');
      const title = head.querySelector('.group-title');
      const desc = head.querySelector('.group-desc');
      const count = head.querySelector('.group-count');
      const titleRect = title.getBoundingClientRect();
      const descRect = desc.getBoundingClientRect();
      const countRect = count.getBoundingClientRect();
      return {
        title: title.childNodes[0]?.textContent.trim() || '',
        badge: head.querySelector('.group-badge')?.textContent.trim() || '',
        desc: desc.textContent.trim(),
        sameLine: Math.abs(titleRect.top - descRect.top) < 6 && descRect.left >= titleRect.right - 2,
        descRightOfTitle: descRect.left >= titleRect.right - 2,
        countRightOfDesc: countRect.left >= descRect.right - 2,
      };
    });
    return {
      groups: groups.length,
      scopeH: Math.round(scope.getBoundingClientRect().height),
      scopeBoxH: Math.round(scopeBox.height),
      firstGroupVisible: groups.length ? Math.round(groups[0].getBoundingClientRect().height) : 0,
      firstGroupHeadH: groups.length
        ? Math.round(groups[0].querySelector('.agent-tool-group-head').getBoundingClientRect().height) : 0,
      groupTitles: groupInfo.map((item) => item.title),
      groupsNotSingleLine: groupInfo.filter((item) => !item.sameLine).map((item) => item.title),
      groupsWithoutDesc: groupInfo.filter((item) => !item.desc).map((item) => item.title),
      groupsCountMisplaced: groupInfo.filter((item) => !item.countRightOfDesc).map((item) => item.title),
      visionGroup: groupInfo.find((item) => item.title === '视觉与图片') || null,
      badges: groupInfo.map((item) => item.badge),
    };
  });
}

async function waitForClosed(page, timeout = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const open = await page.evaluate(() => Boolean(document.querySelector('#agentDialog')?.open));
    if (!open) return true;
    await page.waitForTimeout(150);
  }
  return false;
}

async function waitForCardCount(page, expected, timeout = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const snapshot = await cardSnapshot(page);
    if (snapshot && snapshot.agents.length === expected) return snapshot;
    await page.waitForTimeout(200);
  }
  return cardSnapshot(page);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const badResponses = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  // `Failed to load resource` 这类 console 文案**不带 URL**，无法按 URL 过滤 —— 与本仓其它冒烟同口径：
  // 它不计数，改由下面的 badResponses（带真实 URL）来判"失败请求"。
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    if (/Failed to load resource/i.test(msg.text())) return;
    pageErrors.push(`console.error: ${msg.text()}`);
  });
  page.on('response', (resp) => {
    if (resp.status() >= 400) badResponses.push(`${resp.status()} ${resp.url()}`);
  });
  page.on('dialog', (dialog) => dialog.accept());

  try {
    await cleanupSeeded();
    const bootstrap = await apiJson('/api/bootstrap');
    const agentCount = (bootstrap.agents || []).length;

    // 遗留副本（dsh-*）：早已不在出厂清单里 ⇒ 迁移后不应再带 built_in，且必须可删。
    const legacy = (bootstrap.agents || []).find((agent) => agent.id === 'dsh-standard');
    check('遗留内置副本仍在列表里（内容不丢）', Boolean(legacy), JSON.stringify((bootstrap.agents || []).map((a) => a.id)));
    check('遗留副本的 built_in 标记已摘除', Boolean(legacy) && !legacy.built_in, JSON.stringify(legacy));
    // 2.8.9-beta 起出厂 6 样重新启用（内置区恒在最前，顺序钉死）。
    const BUILT_IN_ORDER = ['tutor', 'master', 'director', 'prompter', 'coder', 'skills'];
    const agentIds = (bootstrap.agents || []).map((agent) => String(agent.id));
    check('出厂内置 6 样齐全且排在最前',
      JSON.stringify(agentIds.slice(0, BUILT_IN_ORDER.length)) === JSON.stringify(BUILT_IN_ORDER),
      JSON.stringify(agentIds));
    const builtInWithEmoji = (bootstrap.agents || [])
      .filter((agent) => agent.built_in)
      .every((agent) => String(agent.avatar || '').length <= 8 && !String(agent.avatar || '').endsWith('.webp'));
    check('内置 Agent 带 emoji 头像（不是上传文件名）', builtInWithEmoji, JSON.stringify(
      (bootstrap.agents || []).filter((a) => a.built_in).map((a) => [a.id, a.avatar])));
    const dshInjected = (bootstrap.agents || []).filter((agent) => /^dsh-/.test(String(agent.id)) && agent.id !== 'dsh-standard');
    check('dsh-code/minimal/cordis 不再出现', dshInjected.length === 0, JSON.stringify(dshInjected.map((a) => a.id)));

    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(1000);
    // 全新隔离实例没配可用供应商时会弹首启引导，它盖在设置按钮上 ⇒ 点击超时。
    // 直接**摘掉节点**：`dialog.close()` 不够——没有可用供应商时引导会自己再开（实测踩过）。
    // 对已配好供应商的正常实例，这个节点本来就不存在，此处是空操作。
    await page.evaluate(() => document.querySelector('#onboardingDialog')?.remove());
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForTimeout(500);

    const cards = await cardSnapshot(page);
    const agentById = new Map((bootstrap.agents || []).map((agent) => [agent.id, agent]));
    check('卡片容器渲染 Agent 卡片 + 新增卡', Boolean(cards) && cards.total >= 1, JSON.stringify(cards));
    check('「新增 Agent」卡片固定在最后一张', cards?.addIsLast === true && cards.addText.includes('新增 Agent'), JSON.stringify(cards));
    check('一行最多三张卡片（grid 三列）', cards?.columns === 3, String(cards?.columns));
    check('卡片数量与数据一致', cards?.agents.length === agentCount, JSON.stringify({ cards: cards?.agents.length, agentCount }));
    check('每张卡片都有名称/固定 Skill 行/提示词摘要',
      cards.agents.every((card) => card.name && card.meta && card.prompt),
      JSON.stringify(cards.agents));
    check('卡片整张可点（手型光标）',
      cards.agents.every((card) => card.cursor === 'pointer'),
      JSON.stringify(cards.agents.map((card) => card.cursor)));
    check('卡片上不再有「默认」角标', cards.agents.every((card) => !card.badges.includes('默认')),
      JSON.stringify(cards.agents.map((card) => card.badges)));
    // 「固定 Skill」下方显示该 Agent 工具集对应的预设名（与后端 tool_scope 逐张核对）。
    const catalog = await (await fetch(`${BASE}/api/tool_catalog`)).json();
    const presets = catalog.presets || [];
    const knownTools = new Set((catalog.tools || []).map((tool) => tool.name));
    const expectedLabel = (scope) => {
      const raw = Array.isArray(scope) ? scope.filter(Boolean) : [];
      if (!raw.length) return '未限制（全部工具）';
      // 与前端 toolScopeLabel 同口径：退役/掉线的工具名不参与匹配与计数。
      const tools = knownTools.size ? raw.filter((name) => knownTools.has(name)) : raw;
      if (!tools.length) return `自定义 · ${raw.length} 个工具`;
      const hit = presets.find((preset) => (preset.tools || []).length === tools.length
        && (preset.tools || []).every((name) => tools.includes(name)));
      return hit ? hit.name : `自定义 · ${tools.length} 个工具`;
    };
    check('每张卡片都在 Skill 下方显示工具集标签',
      cards.agents.every((card) => card.tools.startsWith('工具集：')), JSON.stringify(cards.agents.map((card) => card.tools)));
    check('工具集标签与后端 tool_scope 对应的预设名逐张一致',
      cards.agents.every((card) => card.tools === `工具集：${expectedLabel(agentById.get(card.id)?.tool_scope)}`),
      JSON.stringify(cards.agents.map((card) => [card.id, card.tools, expectedLabel(agentById.get(card.id)?.tool_scope)])));
    check('默认 Agent 不再带高亮类（is-default）',
      cards.agents.every((card) => !card.className.includes('is-default')),
      JSON.stringify(cards.agents.map((card) => card.className)));
    check('所有卡片底色/描边一致（没有哪张看起来像"被选中"）',
      new Set(cards.agents.map((card) => `${card.background}|${card.borderColor}`)).size === 1,
      JSON.stringify(cards.agents.map((card) => [card.id, card.background, card.borderColor])));
    check('「内置」徽标只出现在出厂 6 样上',
      cards.agents.every((card) => card.badges.includes('内置') === BUILT_IN_ORDER.includes(card.id)),
      JSON.stringify(cards.agents.map((card) => [card.id, card.badges])));
    check('内置 Agent 卡片没有 × 删除按钮',
      cards.agents.filter((card) => BUILT_IN_ORDER.includes(card.id)).every((card) => card.hasDelete === false),
      JSON.stringify(cards.agents.filter((c) => BUILT_IN_ORDER.includes(c.id)).map((c) => [c.id, c.hasDelete])));
    const legacyCard = cards.agents.find((card) => card.id === 'dsh-standard');
    check('遗留副本卡片有 × 删除按钮', Boolean(legacyCard) && legacyCard.hasDelete === true, JSON.stringify(legacyCard));
    check('旧的「编辑」按钮已移除', (await dialogSnapshot(page)).legacyEditButton === false, '');

    // 点卡片 → 弹出并加载该 Agent（挑一个带系统提示词的，才能验证提示词也回填）
    const target = cards.agents.find((card) => String(agentById.get(card.id)?.system_prompt || '').trim())
      || cards.agents.find((card) => card.id !== 'dsh-standard')
      || cards.agents[0];
    const expectedPrompt = String(agentById.get(target.id)?.system_prompt || '');
    // 名字要比**后端数据里的 name**，不能比卡片的 `.agent-card-name` 文本：
    // 卡片的 markup 是 `${avatarHtml}${name}`——内置 Agent 的 emoji 头像就嵌在这个 span 里，
    // 所以它的 textContent 天然带前缀（「📘教程助手 Agent」）。拿卡片文本比会误报。
    const expectedName = String(agentById.get(target.id)?.name || '');
    await page.click(`[data-agent-card="${target.id}"] .agent-card-name`);
    await page.waitForTimeout(400);
    let dialog = await dialogSnapshot(page);
    check('点卡片弹出 Agent 设置弹层', dialog.open === true, JSON.stringify(dialog));
    check('弹层已加载该 Agent 的预设内容',
      dialog.name === expectedName && dialog.formId === target.id && dialog.prompt === expectedPrompt,
      JSON.stringify({ dialog, target, expectedName, expectedPrompt }));
    check('工具集与固定 Skill 选择器都在弹层里',
      dialog.hasSkillPicker === true && dialog.hasToolScope === true, JSON.stringify(dialog));
    check('表单里已没有手填 Agent ID 的输入框', dialog.hasIdField === false, String(dialog.hasIdField));
    check('副标题显示已有 Agent 的 ID',
      dialog.subtitle.includes(target.id), JSON.stringify(dialog.subtitle));
    check('有「自定义头像」按钮', dialog.hasAvatarButton === true, '');

    // 固定 Skill / 工具集必须真的可见、可展开、可勾选（分区切换后各自切页再断言）
    const lists = await skillSnapshot(page);
    check('固定 Skill 以卡片形式渲染（复选框 + 名称 + 说明）',
      lists.skillCards > 0 && lists.legacySkillItems === 0 && lists.skillCardBorder === '1px'
      && parseFloat(lists.skillCardRadius) > 0,
      JSON.stringify(lists));
    check('固定 Skill 卡片两列网格',
      lists.skillCardColumns === 2, String(lists.skillCardColumns));
    check('固定 Skill 列表可见且有内容',
      lists.skillCards > 0 && lists.listH > 0 && lists.boxH >= lists.listH,
      JSON.stringify(lists));
    const tools = await toolSnapshot(page);
    check('固定 Skill 与工具集列表都占满各自分区（分区同高）',
      lists.listH > 200 && tools.scopeH > 200 && lists.boxH === tools.scopeBoxH,
      JSON.stringify({ skillListH: lists.listH, skillPanelH: lists.boxH,
        scopeH: tools.scopeH, toolPanelH: tools.scopeBoxH }));
    check('工具集分类可见（未被行高裁掉）',
      tools.groups > 0 && tools.scopeH > 0 && tools.scopeBoxH >= tools.scopeH && tools.firstGroupVisible > 0,
      JSON.stringify(tools));
    check('工具集分组行未被压扁（行高 ≥ 分组头高度）',
      tools.firstGroupVisible >= tools.firstGroupHeadH && tools.firstGroupVisible >= 30,
      JSON.stringify({ group: tools.firstGroupVisible, head: tools.firstGroupHeadH }));
    check('分组头一行呈现（小字说明与标题同行、计数在最后）',
      tools.groupsNotSingleLine.length === 0 && tools.groupsCountMisplaced.length === 0,
      JSON.stringify({ notSingleLine: tools.groupsNotSingleLine, countMisplaced: tools.groupsCountMisplaced }));
    check('每个分组都有小字说明',
      tools.groupsWithoutDesc.length === 0, JSON.stringify(tools.groupsWithoutDesc));
    check('视觉分组存在且带说明（分类收敛为「视觉与图片」）',
      Boolean(tools.visionGroup) && Boolean(tools.visionGroup.desc),
      JSON.stringify({ order: tools.groupTitles, vision: tools.visionGroup }));
    check('每个分组都带风险徽标',
      tools.badges.length === tools.groups && tools.badges.every((badge) => badge),
      JSON.stringify(tools.badges));
    const expand = await page.evaluate(() => {
      const group = document.querySelector('#agentToolScope .agent-tool-group');
      const head = group.querySelector('.agent-tool-group-head');
      head.click();
      const grid = group.querySelector('.permission-grid');
      const box = grid.getBoundingClientRect();
      const cb = grid.querySelector('input[type="checkbox"]');
      const before = cb.checked;
      cb.click();
      return {
        expanded: !group.classList.contains('collapsed'),
        gridVisible: grid.getBoundingClientRect().height > 0,
        gridHeight: Math.round(box.height),
        checkboxToggled: cb.checked !== before,
      };
    });
    check('点分类表头可展开工具列表', expand.expanded === true && expand.gridVisible === true && expand.gridHeight > 0,
      JSON.stringify(expand));
    check('展开后可以勾选工具', expand.checkboxToggled === true, JSON.stringify(expand));

    await page.keyboard.press('Escape');
    await page.waitForTimeout(400);
    dialog = await dialogSnapshot(page);
    const settingsOpen = await page.evaluate(() => Boolean(document.querySelector('#settingsDialog')?.open));
    check('Esc 关闭弹层且设置页仍打开', dialog.open === false && settingsOpen === true,
      JSON.stringify({ open: dialog.open, settingsOpen }));

    // 新增卡片 → 新建（不再填 ID，后台自动分配）
    await page.click('[data-agent-add]');
    await page.waitForTimeout(400);
    dialog = await dialogSnapshot(page);
    check('点「新增 Agent」卡片打开空表单（无 ID 输入）',
      dialog.open === true && dialog.name === '' && dialog.title.includes('新增') && dialog.hasIdField === false,
      JSON.stringify(dialog));
    check('新建时提示 ID 由后台分配', dialog.subtitle.includes('自动分配'), JSON.stringify(dialog.subtitle));
    // 表单默认停在「基本」页（名称与系统提示词同页），直接填即可。
    await page.fill('#agentName', `${PREFIX}卡片`);
    await page.fill('#agentSystemPromptEdit', '这是冒烟测试用的 Agent 提示词，用来验证卡片摘要渲染。');
    await page.click('#saveAgentForm');
    const closed = await waitForClosed(page);
    const afterAdd = await waitForCardCount(page, agentCount + 1);
    check('保存后弹层关闭', closed === true, '');
    check('新 Agent 卡片出现（数量 +1）',
      afterAdd?.agents.length === agentCount + 1,
      JSON.stringify({ before: agentCount, after: afterAdd?.agents.length }));
    const created = afterAdd.agents.find((card) => card.name === `${PREFIX}卡片`);
    check('后台自动分配了持久化唯一 ID',
      Boolean(created) && /^agent_[0-9a-f]{12}$/.test(created.id),
      JSON.stringify(created));
    check('新卡片显示名称与提示词摘要',
      Boolean(created) && created.prompt.includes('冒烟测试'),
      JSON.stringify(created));
    // 新建 Agent 的工具集 = 后端默认选中集（= 标准模式），卡片上必须显示预设名。
    check('新卡片显示「工具集：标准模式」（预设名映射真的生效）',
      Boolean(created) && created.tools === '工具集：标准模式', JSON.stringify(created?.tools));

    // × 删除（确认后生效）
    await page.click(`[data-agent-delete="${created.id}"]`);
    const afterDelete = await waitForCardCount(page, agentCount);
    check('× 删除后卡片消失（数量回落）',
      afterDelete?.agents.length === agentCount,
      JSON.stringify({ after: afterDelete?.agents.length, expected: agentCount }));

    // 遗留副本也能删（验证后端不再拦内置 id）
    await page.click('[data-agent-delete="dsh-standard"]');
    const afterLegacyDelete = await waitForCardCount(page, agentCount - 1);
    check('遗留内置副本可以删掉（后端不再拦 dsh- id）',
      afterLegacyDelete?.agents.length === agentCount - 1 && !afterLegacyDelete.agents.some((card) => card.id === 'dsh-standard'),
      JSON.stringify(afterLegacyDelete?.agents.map((card) => card.id)));

    // 窄屏列数降级
    await page.setViewportSize({ width: 900, height: 800 });
    await page.waitForTimeout(300);
    check('900px 窗口降级为两列', (await cardSnapshot(page))?.columns === 2, '');
    await page.setViewportSize({ width: 640, height: 800 });
    await page.waitForTimeout(300);
    check('640px 窗口降级为单列', (await cardSnapshot(page))?.columns === 1, '');
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.waitForTimeout(200);

    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
    // 失败请求按 URL 判。**夹具固有噪音**（与 `verify/tool_groups_smoke.py` 同口径）：
    // `/api/providers/models` 是**主动出网**拉模型列表的端点，隔离实例里的占位供应商
    // （`https://example.invalid/v1`、Key 为空）必然回 400；本冒烟验的是 Agent 卡片，与供应商连通性无关。
    const realBadResponses = badResponses.filter((line) => !/\/api\/providers\/models/i.test(decodeURIComponent(line)));
    check('零非预期 4xx/5xx（供应商连通性噪音除外）', realBadResponses.length === 0,
      realBadResponses.slice(0, 3).join(' | '));
  } catch (error) {
    const stack = String(error && error.stack ? error.stack : error).split('\n').slice(0, 3).join(' | ');
    check('冒烟执行未抛异常', false, stack);
  } finally {
    await cleanupSeeded().catch(() => {});
    await browser.close();
  }

  console.log();
  console.log(`Agent 卡片冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
