// Agent 设置页「快捷提示词（套用 / 另存 / × 删除）+ 角色卡追加导入」冒烟（源码 server，端口 8790）。
// 前置：python verify/make_test_card.py && python server.py --port 8790
// 运行：$env:NODE_PATH="<node_modules 目录>"; node verify\agent_prompt_smoke.cjs
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const CARD = path.resolve(__dirname, '..', 'data', 'generated', 'seed_fixture', '收藏冒烟角色卡.png');
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(url, options = {}) {
  const response = await fetch(`${BASE}${url}`, { headers: { 'Content-Type': 'application/json' }, ...options });
  return response.json().catch(() => ({}));
}

async function presetTitles() {
  const data = await apiJson('/api/conversation-prompt-presets');
  return (data.presets || []).map((item) => item.title);
}

(async () => {
  // 播种一条快捷提示词（冒烟后删除，避免污染用户配置）
  const presetText = '你是冒烟测试用的提示词。';
  const created = await apiJson('/api/conversation-prompt-presets', {
    method: 'POST',
    body: JSON.stringify({ title: '收藏冒烟提示词', system_prompt: presetText }),
  });
  const presetId = created?.preset?.id || '';
  check('已播种快捷提示词', Boolean(presetId), JSON.stringify(created).slice(0, 160));

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  // 8790 的隔离配置（verify/_tmp_agent_e2e/config.json）里 providers 只有占位 base_url、没配 Key，
  // hasUsableProvider() 为 false ⇒ 每个全新浏览器 profile 首启都会弹 #onboardingDialog
  //（19-onboarding.js maybeShowOnboarding：localStorage 的 dismissed 标记或「有可用供应商」
  // 二选一才放行）。原生 <dialog>.showModal 的 top layer 会拦掉后续所有点击（Playwright 报
  // "dialog intercepts pointer events"）。与 tool_groups_smoke 同款做法：initScript 先置
  // dismissed 标记，页面加载后再把节点摘掉（没有可用供应商时引导可能自己再开，实测踩过）。
  await page.addInitScript(() => {
    try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (error) { void error; }
  });
  const pageErrors = [];
  const notFound = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  // 「Failed to load resource」这条 console 文案不带 URL，没法与已知噪音区分：8790 的占位
  // 供应商（probe-te / probe-moda，base_url=example.invalid）让设置页的模型目录探测必然
  // POST /api/providers/models 400 —— 与 _check_tool_cards_compact / tool_groups_smoke 同款豁免；
  // 真正的资源问题仍由下面的 notFound（逐条带 URL 的 404）兜住。
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !msg.text().includes('Failed to load resource')) {
      pageErrors.push(`console.error: ${msg.text()}`);
    }
  });
  page.on('response', (res) => { if (res.status() === 404) notFound.push(res.url()); });
  page.on('dialog', (dialog) => dialog.accept());

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
    // 引导节点防御性摘除：只要它还挂在 top layer，下面 #openSettings 的点击就会被它拦截。
    await page.evaluate(() => document.querySelector('#onboardingDialog')?.remove());

    // 设置页导航已无「快捷提示词」入口
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    const navHasPrompts = await page.evaluate(() => Boolean(document.querySelector('[data-settings-tab="conversation-prompts"]')));
    check('设置页导航已无「快捷提示词」入口', navHasPrompts === false, String(navHasPrompts));

    await page.click('[data-settings-tab="agent"]');
    await page.waitForSelector('#agentCards .agent-card', { timeout: 10000 });
    await page.click('#agentCards [data-agent-card] .agent-card-name');
    await page.waitForSelector('#agentDialog[open]', { timeout: 10000 });
    await page.waitForTimeout(200);
    // 系统提示词相关字段（#agentSystemPromptEdit / 快捷提示词按钮 / 角色卡导入 / .agent-prompt-block）
    // 全部搬到 data-agent-panel="prompt" 面板，默认 hidden。本脚本整份都在操作这些字段，
    // 打开弹层后先切一次「提示词」tab，后续断言/交互保持不动。
    await page.click('[data-agent-tab="prompt"]');
    await page.waitForTimeout(200);

    const wiring = await page.evaluate(() => {
      const block = document.querySelector('.agent-prompt-block');
      const head = document.querySelector('.agent-prompt-head');
      const dialog = document.querySelector('#agentDialog');
      return {
        hasButton: Boolean(document.querySelector('#agentPromptPresetButton')),
        hasSaveButton: Boolean(document.querySelector('#saveAgentPromptPreset')),
        hasPanelInDialog: Boolean(dialog && dialog.querySelector('#agentPromptPresetPanel')),
        hasImport: Boolean(document.querySelector('#importAgentCharacterCard')),
        hasFileInput: Boolean(document.querySelector('#agentCharacterCardFileInput')),
        hasBlock: Boolean(block),
        toolbarInsideBlock: Boolean(block && block.querySelector('#agentPromptPresetButton')),
        titleInsideHead: Boolean(head && head.querySelector('.agent-prompt-title')),
        hint: document.querySelector('.agent-prompt-block .agent-prompt-hint')?.textContent || '',
        importTitle: document.querySelector('#importAgentCharacterCard')?.getAttribute('title') || '',
      };
    });
    check('表单有「套用快捷提示词」按钮', wiring.hasButton, JSON.stringify(wiring));
    check('表单有「存为快捷提示词」按钮', wiring.hasSaveButton, JSON.stringify(wiring));
    check('面板挂在 Agent 弹层内部（top layer 不被盖住）', wiring.hasPanelInDialog, JSON.stringify(wiring));
    check('导入角色卡按钮 + 文件输入就位', wiring.hasImport && wiring.hasFileInput, JSON.stringify(wiring));
    check('系统提示词成块：工具栏在标题行内', wiring.hasBlock && wiring.toolbarInsideBlock && wiring.titleInsideHead,
      JSON.stringify(wiring));
    check('导入按钮说明「追加不覆盖」',
      wiring.importTitle.includes('追加') && wiring.importTitle.includes('不覆盖'), wiring.importTitle);

    // 0) 空提示词点「存为快捷提示词」→ 底部提示必须显示在模态弹层之上（top layer 遮挡回归）
    // 前提要自己摆出来：本节测的是「空提示词」分支（08-conversations.js
    // openAgentPromptPresetSaveDialog：空 → toast「系统提示词为空」，非空 → 直接打开另存弹窗）。
    // 共享实例的第一张卡片（📘教程助手 Agent）带着非空出厂提示词，不先清空就会走另一条分支、
    // 把 #promptPresetDialog 留在 top layer 拦掉后续所有交互。清空只改表单内存值，不点
    // 「保存 Agent」就不落库，浏览器一关即弃。
    await page.fill('#agentSystemPromptEdit', '');
    await page.click('#saveAgentPromptPreset');
    await page.waitForTimeout(400);
    const toastState = await page.evaluate(() => {
      const el = document.querySelector('#toast');
      if (!el) return { exists: false };
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return {
        exists: true,
        text: el.textContent.trim(),
        visible: rect.width > 0 && rect.height > 0 && style.opacity === '1',
        parentIsTopDialog: el.parentElement?.matches('dialog[open]:modal') === true,
      };
    });
    check('空提示词点另存会弹提示', toastState.text.includes('系统提示词为空'), JSON.stringify(toastState));
    check('提示框挂在最上层模态弹层内且可见（不再被盖住）',
      toastState.visible === true && toastState.parentIsTopDialog === true, JSON.stringify(toastState));

    // 1) 角色卡导入 = 追加（不覆盖已有内容）
    await page.fill('#agentSystemPromptEdit', '原始规则：保持简短。');
    await page.setInputFiles('#agentCharacterCardFileInput', CARD);
    await page.waitForTimeout(1200);
    const afterImport = await page.inputValue('#agentSystemPromptEdit');
    check('导入后保留原有内容', afterImport.startsWith('原始规则：保持简短。'), afterImport.slice(0, 60));
    check('导入内容追加到末尾（空行分隔）', afterImport.includes('\n\n') && afterImport.includes('姓名：收藏冒烟角色'), afterImport.slice(0, 200));
    check('角色卡描述被解析', afterImport.includes('沉默寡言的图书馆管理员'), afterImport.slice(0, 200));

    // 2) 面板：打开 → 条目带 × 删除按钮 → 点条目套用（覆盖前确认，dialog 已自动 accept）
    await page.click('#agentPromptPresetButton');
    await page.waitForTimeout(500);
    const panel = await page.evaluate(() => {
      const el = document.querySelector('#agentPromptPresetPanel');
      const items = [...(el?.querySelectorAll('.quick-msg-item') || [])];
      return {
        open: Boolean(el && !el.hidden),
        expanded: document.querySelector('#agentPromptPresetButton')?.getAttribute('aria-expanded'),
        titles: items.map((item) => item.querySelector('b')?.textContent.trim() || ''),
        deletes: items.filter((item) => item.querySelector('[data-agent-preset-delete]')).length,
        panelInsideDialog: Boolean(el && el.closest('#agentDialog')),
      };
    });
    check('点按钮展开面板', panel.open === true && panel.expanded === 'true', JSON.stringify(panel));
    check('面板条目已载入', panel.titles.includes('收藏冒烟提示词'), JSON.stringify(panel.titles));
    check('每条条目都有 × 删除按钮', panel.deletes === panel.titles.length && panel.deletes > 0, JSON.stringify(panel));
    check('面板在 Agent 弹层内部', panel.panelInsideDialog === true, '');

    await page.click(`[data-agent-preset="${presetId}"] .quick-msg-main`);
    await page.waitForTimeout(500);
    const afterPreset = await page.inputValue('#agentSystemPromptEdit');
    check('点条目套用（覆盖为预设文本）', afterPreset === presetText, afterPreset.slice(0, 120));
    const closedAfterApply = await page.evaluate(() => document.querySelector('#agentPromptPresetPanel')?.hidden === true);
    check('套用后面板自动收起', closedAfterApply === true, '');

    // 3) 存为快捷提示词：正文预填当前文本框，标题默认取首行
    await page.fill('#agentSystemPromptEdit', '你是「另存冒烟」提示词。');
    await page.click('#saveAgentPromptPreset');
    await page.waitForTimeout(400);
    const saveDialog = await page.evaluate(() => ({
      open: Boolean(document.querySelector('#promptPresetDialog')?.open),
      title: document.querySelector('#promptPresetTitle')?.value || '',
      text: document.querySelector('#promptPresetText')?.value || '',
      hint: document.querySelector('#promptPresetHint')?.textContent || '',
      dialogTitle: document.querySelector('#promptPresetDialogTitle')?.textContent || '',
    }));
    check('点「存为快捷提示词」弹出标题+正文弹窗', saveDialog.open === true, JSON.stringify(saveDialog));
    check('标题默认取正文首行、提示显示字符数',
      saveDialog.title.includes('另存冒烟') && saveDialog.hint.includes('字符'), JSON.stringify(saveDialog));
    check('正文预填当前系统提示词', saveDialog.text === '你是「另存冒烟」提示词。', JSON.stringify(saveDialog.text));
    await page.fill('#promptPresetTitle', '另存冒烟提示词');
    await page.click('#savePromptPreset');
    await page.waitForTimeout(900);
    const titlesAfterSave = await presetTitles();
    check('新快捷提示词已入库', titlesAfterSave.includes('另存冒烟提示词'), JSON.stringify(titlesAfterSave));

    // 4) ✎ 编辑：改标题与正文，保存后库内同步更新
    await page.click('#agentPromptPresetButton');
    await page.waitForTimeout(500);
    const savedId = await page.evaluate(() => {
      const items = [...document.querySelectorAll('#agentPromptPresetPanel .quick-msg-item')];
      const hit = items.find((item) => item.querySelector('b')?.textContent.trim() === '另存冒烟提示词');
      return hit ? hit.dataset.agentPreset : '';
    });
    check('面板里能看到刚存的条目', Boolean(savedId), savedId);
    if (savedId) {
      await page.click(`[data-agent-preset-edit="${savedId}"]`);
      await page.waitForTimeout(400);
      const editDialog = await page.evaluate(() => ({
        open: Boolean(document.querySelector('#promptPresetDialog')?.open),
        dialogTitle: document.querySelector('#promptPresetDialogTitle')?.textContent || '',
        title: document.querySelector('#promptPresetTitle')?.value || '',
        text: document.querySelector('#promptPresetText')?.value || '',
      }));
      check('点 ✎ 打开编辑弹窗并回填标题+正文',
        editDialog.open === true && editDialog.title === '另存冒烟提示词'
        && editDialog.text === '你是「另存冒烟」提示词。', JSON.stringify(editDialog));
      check('编辑态弹窗标题为「编辑快捷提示词」', editDialog.dialogTitle.includes('编辑'), editDialog.dialogTitle);
      await page.fill('#promptPresetTitle', '另存冒烟提示词改');
      await page.fill('#promptPresetText', '编辑后的正文。');
      await page.click('#savePromptPreset');
      await page.waitForTimeout(900);
      const afterEdit = await presetTitles();
      check('编辑后标题已更新且不新增条目',
        afterEdit.includes('另存冒烟提示词改') && !afterEdit.includes('另存冒烟提示词'),
        JSON.stringify(afterEdit));
      const editedText = await page.evaluate(() => {
        const items = [...document.querySelectorAll('#agentPromptPresetPanel .quick-msg-item')];
        const hit = items.find((item) => item.querySelector('b')?.textContent.trim() === '另存冒烟提示词改');
        return hit ? hit.querySelector('small')?.textContent.trim() || '' : '';
      });
      check('面板预览同步为新正文', editedText.includes('编辑后的正文'), editedText);

      // 5) × 删除（确认框已自动 accept）
      await page.click(`[data-agent-preset-delete="${savedId}"]`);
      await page.waitForTimeout(900);
      const titlesAfterDelete = await presetTitles();
      check('× 删除后条目从库里消失', !titlesAfterDelete.includes('另存冒烟提示词改'), JSON.stringify(titlesAfterDelete));
      const stillInPanel = await page.evaluate(() => [...document.querySelectorAll('#agentPromptPresetPanel .quick-msg-item b')]
        .map((el) => el.textContent.trim()));
      check('× 删除后面板列表同步刷新', !stillInPanel.includes('另存冒烟提示词改'), JSON.stringify(stillInPanel));
    }

    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
    check('零 404 资源', notFound.length === 0, notFound.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
    if (presetId) {
      await apiJson(`/api/conversation-prompt-presets/${encodeURIComponent(presetId)}`, { method: 'DELETE' });
      console.log(`（已清理冒烟快捷提示词 ${presetId}）`);
    }
  }

  console.log();
  console.log(`Agent 提示词冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  // 不用 process.exit：undici 的 keep-alive 连接未关时强退会触发 libuv 断言（Windows/Node 24）。
  process.exitCode = failures.length ? 1 : 0;
})();
