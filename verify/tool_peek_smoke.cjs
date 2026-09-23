// 工具集清单冒烟：已配好的 Agent 再次打开时，工具集页必须能看清「用了哪些工具」（§九.133）。
// 覆盖两条改动：①卡片态「当前已选工具」只读清单；②编辑态「只看已选」视图（含全选框语义）。
// 前置：由 tool_peek_smoke.py 自编排起隔离实例（独立 data_dir + 端口，默认 8805）——本脚本会
// 往那个实例写一个工具集与一个 Agent，**不要直接对着真数据目录的实例跑**。
// 手跑：先 `NAIBA_TMP_PORT=8805 NAIBA_TMP_ROOT=verify/_tmp_tool_peek .venv\Scripts\python.exe verify\_serve_tmp.py`，
// 再 `$env:NODE_PATH="<node_modules>"; $env:NAIBA_TMP_BASE="http://127.0.0.1:8805"; node verify\tool_peek_smoke.cjs`。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_TMP_BASE || 'http://127.0.0.1:8799';
const SET_NAME = '探针工具集（长对话）';
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

(async () => {
  const catalog = await apiJson('/api/tool_catalog');
  const allTools = (catalog.tools || []).map((tool) => tool.name);
  // 挑一套跨分类的工具（有 MCP 就排除，隔离实例没连 MCP）：24 个，模拟"自定义好的 Agent"。
  const picked = allTools.filter((name) => !name.startsWith('mcp__')).slice(0, 24);
  const groupOf = new Map();
  for (const group of catalog.groups || []) {
    for (const name of group.tools || []) groupOf.set(name, group.name);
  }
  const wantedGroups = new Set(picked.map((name) => groupOf.get(name)).filter(Boolean));

  await apiJson(`/api/tool_sets`, { method: 'POST', body: { id: '', name: SET_NAME, tools: picked } });
  const saved = await apiJson('/api/agents', {
    method: 'POST',
    body: { id: '', name: '探针Agent（已配好）', system_prompt: '探针用的系统提示词。', skill_ids: [], tool_scope: picked },
  });
  const agentId = saved.id;
  console.log(`[probe] agent=${agentId} tools=${picked.length} groups=${wantedGroups.size}`);

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));

  try {
    await page.addInitScript(() => {
      try { localStorage.setItem('naibaOnboardingDismissed', '1'); } catch (error) { void error; }
    });
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.evaluate(() => import('./js/01-core.js').then((m) => { m.state.syncPolling = false; }))
      .catch(() => {});
    await page.waitForTimeout(600);
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForTimeout(500);

    // 打开「已配好的 Agent」——这一步就是用户说的"再次打开"
    await page.click(`[data-agent-card="${agentId}"] .agent-card-name`);
    await page.waitForTimeout(500);
    await page.click('[data-agent-tab="tools"]');
    await page.waitForTimeout(400);

    const cardView = await page.evaluate(() => {
      const peek = document.querySelector('#agentToolPeekList');
      const box = peek.getBoundingClientRect();
      return {
        summary: document.querySelector('#agentToolPresetState')?.textContent.trim() || '',
        peekExists: Boolean(peek),
        peekVisible: Boolean(peek) && !peek.hidden && box.height > 0,
        peekH: Math.round(box.height),
        groups: [...peek.querySelectorAll('.tool-peek-group')].map((row) => ({
          name: row.querySelector('b')?.textContent.trim() || '',
          tools: (row.querySelector('span')?.textContent || '').split('、').filter(Boolean),
        })),
        toggleText: document.querySelector('#toggleAgentToolPeek')?.textContent.trim() || '',
        snippet: (peek.textContent || '').slice(0, 120),
      };
    });
    check('卡片态存在「当前已选工具」清单且默认展开',
      cardView.peekExists && cardView.peekVisible && cardView.peekH > 0, JSON.stringify(cardView));
    check('摘要仍显示这一套的名字与总数',
      cardView.summary.includes(SET_NAME) && cardView.summary.includes(`${picked.length} 个工具`),
      JSON.stringify(cardView.summary));
    const peekedTools = cardView.groups.flatMap((row) => row.tools);
    check('清单里的工具名与 Agent 的 tool_scope 完全一致（不多不少）',
      peekedTools.length === picked.length
      && [...peekedTools].sort().join() === [...picked].sort().join(),
      JSON.stringify({ peeked: peekedTools.length, scope: picked.length,
        missing: picked.filter((n) => !peekedTools.includes(n)),
        extra: peekedTools.filter((n) => !picked.includes(n)) }));
    check('清单按分类分组（覆盖这套工具用到的每个分类）',
      cardView.groups.length === wantedGroups.size
      && cardView.groups.every((row) => wantedGroups.has(row.name)),
      JSON.stringify({ rows: cardView.groups.map((row) => [row.name, row.tools.length]),
        wanted: [...wantedGroups] }));
    await page.screenshot({ path: 'verify/tool_peek_smoke_1_cards.png' });

    // 缺陷前提：编辑态默认分组视图是折叠的，看不到具体工具名 → 这正是原来"不知道配了什么"的原因。
    await page.click(`[data-tool-template-card]`);
    await page.waitForTimeout(500);
    const grouped = await page.evaluate(() => {
      const scope = document.querySelector('#agentToolScope');
      const groups = [...scope.querySelectorAll('.agent-tool-group')];
      return {
        groups: groups.length,
        collapsed: groups.filter((group) => group.classList.contains('collapsed')).length,
        text: scope.innerText,
        checkboxes: scope.querySelectorAll('.permission-grid input[type="checkbox"]').length,
        onlyBtn: document.querySelector('#agentToolOnlySelected')?.textContent.trim() || '',
      };
    });
    check('前提成立：分组视图默认全部折叠、工具名一个都看不见',
      grouped.groups > 0 && grouped.collapsed === grouped.groups && !grouped.text.includes(picked[0]),
      JSON.stringify({ groups: grouped.groups, collapsed: grouped.collapsed, snippet: grouped.text.slice(0, 80) }));
    check('「只看已选 N」按钮显示当前工具数',
      grouped.onlyBtn === `只看已选 ${picked.length}`, grouped.onlyBtn);

    // 只看已选：一键摊开
    await page.click('#agentToolOnlySelected');
    await page.waitForTimeout(400);
    const only = await page.evaluate(() => {
      const scope = document.querySelector('#agentToolScope');
      const groups = [...scope.querySelectorAll('.agent-tool-group')];
      const cbs = [...scope.querySelectorAll('.permission-grid input[type="checkbox"]')];
      return {
        groups: groups.length,
        collapsed: groups.filter((group) => group.classList.contains('collapsed')).length,
        checkboxes: cbs.length,
        allChecked: cbs.every((cb) => cb.checked),
        names: cbs.map((cb) => cb.value),
        pressed: document.querySelector('#agentToolOnlySelected')?.getAttribute('aria-pressed'),
        counter: document.querySelector('#agentToolCount')?.textContent.trim() || '',
      };
    });
    check('只看已选：分组全部展开（不再折叠）', only.collapsed === 0 && only.groups > 0, JSON.stringify(only));
    check('只看已选：列出的工具恰好是已勾选的那一批（无未选工具混入）',
      only.checkboxes === picked.length && [...only.names].sort().join() === [...picked].sort().join(),
      JSON.stringify({ got: only.checkboxes, want: picked.length,
        extra: only.names.filter((n) => !picked.includes(n)) }));
    check('只看已选：列出的每一项都是勾选态', only.allChecked === true, JSON.stringify(only));
    check('只看已选：按钮进入按下态、计数不变',
      only.pressed === 'true' && only.counter.includes(`已选 ${picked.length}`), JSON.stringify(only));
    await page.screenshot({ path: 'verify/tool_peek_smoke_2_only_selected.png' });

    // 关键语义：这一视图里点分类全选框 = 取消该分类已选，绝不能"整组全选"变出新工具
    const toggle = await page.evaluate(() => {
      const scope = document.querySelector('#agentToolScope');
      const before = document.querySelector('#agentToolCount').textContent.trim();
      const group = scope.querySelector('.agent-tool-group');
      const all = group.querySelector('.group-select-all');
      group.querySelector('.agent-tool-group-head').click(); // 先确保是展开态（已是）
      all.click();
      return { before };
    });
    await page.waitForTimeout(400);
    const afterToggle = await page.evaluate(() => {
      const scope = document.querySelector('#agentToolScope');
      const cbs = [...scope.querySelectorAll('.permission-grid input[type="checkbox"]')];
      return {
        counter: document.querySelector('#agentToolCount')?.textContent.trim() || '',
        checkboxes: cbs.length,
        names: cbs.map((cb) => cb.value),
      };
    });
    const afterCount = Number((afterToggle.counter.match(/已选 (\d+)/) || [])[1]);
    check('只看已选里取消整组 = 减少已选，且没有变出新工具',
      afterCount < picked.length && afterCount === afterToggle.checkboxes
      && afterToggle.names.every((name) => picked.includes(name)),
      JSON.stringify({ before: toggle.before, after: afterToggle.counter,
        listed: afterToggle.checkboxes, extra: afterToggle.names.filter((n) => !picked.includes(n)) }));

    // 切回分组视图：全量工具回来（编辑能力不丢）
    await page.click('#agentToolOnlySelected');
    await page.waitForTimeout(400);
    const back = await page.evaluate(() => {
      const scope = document.querySelector('#agentToolScope');
      return {
        groups: scope.querySelectorAll('.agent-tool-group').length,
        collapsed: [...scope.querySelectorAll('.agent-tool-group')].filter((g) => g.classList.contains('collapsed')).length,
        pressed: document.querySelector('#agentToolOnlySelected')?.getAttribute('aria-pressed'),
      };
    });
    check('再点一次回到分组视图（全部折叠、工具目录完整）',
      back.pressed === 'false' && back.collapsed === back.groups && back.groups > 1, JSON.stringify(back));

    check('零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  } catch (error) {
    const stack = String(error && error.stack ? error.stack : error).split('\n').slice(0, 4).join(' | ');
    check('探针执行未抛异常', false, stack);
  } finally {
    if (agentId) await apiJson(`/api/agents/${encodeURIComponent(agentId)}`, { method: 'DELETE' }).catch(() => {});
    await browser.close();
  }

  console.log();
  console.log(`探针结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
