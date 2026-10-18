// MCP 连接器分页 · 真后端冒烟（计划 docs/plans/2026-10-18-MCP连接器分页改造计划.md §六）
//
// 与静态 public 服务的冒烟不同：本脚本要打 /api/mcp 系列接口，必须连**真后端**隔离实例。
// 用法：
//   1) $env:NAIBA_TMP_PORT=8799; $env:NAIBA_TMP_ROOT="verify\_tmp_mcp_probe";
//      .venv\Scripts\python.exe verify\_serve_tmp.py
//   2) $env:NODE_PATH="$env:USERPROFILE\node_modules"; node verify\mcp_connectors_smoke.cjs
//
// 断言顺序刻意「先钉缺陷前提」：
//   · 旧版把「测试/重连/删除」append 到与错误文本同一个 flex 行，长错误会把按钮挤成一坨。
//     这里注入 600 字错误后量**真实行高**并对比普通行：高度必须恒定，且副标题必须被截断
//     （scrollWidth > clientWidth），只看「有没有文本」是测不出来的。
//   · 「局域网访问」页里不许再有任何 MCP 元素（搬走就要搬干净）。
//   · 列表只回显 env 的 KEY：接口响应里出现令牌值即失败。
const { chromium } = require('playwright');
const path = require('path');

const PORT = Number(process.env.NAIBA_MCP_SMOKE_PORT || process.env.NAIBA_TMP_PORT || 8799);
const BASE = `http://127.0.0.1:${PORT}`;
const ROOT = path.resolve(__dirname, '..');
const SHOT_DIR = path.join(ROOT, 'verify');

const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const MISSING_CMD = '__naiba_missing_cmd__';
const SECRET = 'sekret-9f3a-mcp';
const LONG_ERROR = `spawn npx ENOENT —— 找不到可执行文件。${'请确认 npx 已加入 PATH，或把命令写成绝对路径。'.repeat(20)}`;

async function mcpPayload() {
  const response = await fetch(`${BASE}/api/mcp`);
  return response.json();
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));

  await page.goto(`${BASE}/index.html`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#messageInput', { timeout: 15000 });
  // 隔离实例每次都是「全新安装」，首启引导弹窗会拦点击。
  await page.evaluate(() => document.getElementById('onboardingDialog')?.remove());
  await sleep(600);

  const openMcpTab = async () => {
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('[data-settings-tab="mcp"]');
    await sleep(500);
  };
  const closeSettings = async () => {
    await page.click('#settingsDialog [data-close="settingsDialog"]');
    await sleep(400);
  };
  const panelVisible = (name) => page.evaluate((panel) => {
    const element = document.querySelector(`[data-settings-panel="${panel}"]`);
    return Boolean(element) && !element.hidden;
  }, name);

  // ── ① 分页可达 + 旧的「局域网访问」页不再含 MCP ─────────────────────────────
  await openMcpTab();
  check('① 设置里能到达「MCP 连接器」分页', await panelVisible('mcp'));
  const tabTitle = await page.textContent('#settingsToolbarSub');
  check('① 标题栏副标题跟随新分页', String(tabTitle || '').includes('MCP 连接器'), String(tabTitle));
  const connectionsHasMcp = await page.evaluate(() => {
    const panel = document.querySelector('[data-settings-panel="connections"]');
    if (!panel) return 'missing-panel';
    return Boolean(panel.querySelector('#mcpList, #mcpAddNew, .mcp-row')) ? 'has-mcp' : '';
  });
  check('① 「局域网访问」页不含任何 MCP 元素', connectionsHasMcp === '', String(connectionsHasMcp));

  // ── ② 新建（表单模式）：引号感知的参数切分 + env 只存不露 ────────────────────
  await page.click('#mcpAddNew');
  await sleep(250);
  check('② 点「＋ 新增 MCP」切到表单视图', await page.evaluate(() => !document.getElementById('mcpFormView').hidden
    && document.getElementById('mcpListView').hidden));
  const defaultTimeout = await page.inputValue('#mcpFormTimeout');
  check('② 新建表单预填 30000 毫秒', defaultTimeout === '30000', defaultTimeout);

  await page.fill('#mcpFormId', 'smoke-form');
  await page.fill('#mcpFormCommand', MISSING_CMD);
  await page.fill('#mcpFormArgs', `-y "@scope/pkg" "D:\\Program Files\\z"`);
  await page.fill('#mcpFormEnv', JSON.stringify({ SMOKE_TOKEN: SECRET }));
  await page.click('#mcpFormSave');
  await page.waitForFunction(() => document.getElementById('mcpListView') && !document.getElementById('mcpListView').hidden, null, { timeout: 20000 });
  await sleep(600);
  check('② 保存后回到列表并出现新行', await page.evaluate(() => Boolean(document.querySelector('#mcpList .mcp-row[data-mcp-id="smoke-form"]'))));

  const afterCreate = await mcpPayload();
  const created = (afterCreate.servers || []).find((server) => server.id === 'smoke-form') || {};
  check('② 参数按引号切分（带空格路径整块留下）',
    JSON.stringify(created.args) === JSON.stringify(['-y', '@scope/pkg', 'D:\\Program Files\\z']),
    JSON.stringify(created.args));
  check('② 超时按表单值落库', created.start_timeout_ms === 30000, String(created.start_timeout_ms));
  check('② env 只回显 KEY', JSON.stringify(created.env_keys) === JSON.stringify(['SMOKE_TOKEN']),
    JSON.stringify(created.env_keys));
  check('② 接口响应里没有 env 的值（防令牌泄漏）',
    !JSON.stringify(afterCreate).includes(SECRET));

  // ── ③ JSON 模式：两种粘贴格式都能识别 ────────────────────────────────────
  await page.click('#mcpAddNew');
  await sleep(200);
  await page.click('#mcpFormMode [data-mcp-mode="json"]');
  await sleep(200);
  check('③ 切到 JSON 模式后表单字段隐藏', await page.evaluate(() => document.getElementById('mcpFormFields').hidden
    && !document.getElementById('mcpFormJson').hidden));
  await page.fill('#mcpFormJsonText', JSON.stringify({
    mcpServers: { 'smoke-json': { type: 'stdio', command: MISSING_CMD, args: ['-y', 'pkg'] } },
  }, null, 2));
  await page.click('#mcpFormJsonSave');
  await page.waitForFunction(() => document.getElementById('mcpListView') && !document.getElementById('mcpListView').hidden, null, { timeout: 20000 });
  await sleep(600);
  check('③ mcpServers 包一层的 JSON 也能存成一条', await page.evaluate(() => Boolean(document.querySelector('#mcpList .mcp-row[data-mcp-id="smoke-json"]'))));

  // 前端函数的真实行为（不是源码里有没有这个字符串）
  const helpers = await page.evaluate(async () => {
    const module = await import('/js/09-settings.js');
    return {
      args: module.splitArgs('  -y   "@x/y"  "D:\\Program Files\\z"  '),
      quotedEmpty: module.splitArgs('a "" b'),
      json: module.parseMcpJson('{"mcpServers":{"s1":{"command":"npx","args":["-y","a"],"start_timeout_ms":60000}}}'),
      plain: module.parseMcpJson('{"s2":{"command":"node","args":"-y  a"}}'),
      badJson: (() => { try { module.parseMcpJson('{oops'); return 'no-error'; } catch (error) { return error.message; } })(),
      badCommand: (() => { try { module.parseMcpJson('{"s3":{"args":[]}}'); return 'no-error'; } catch (error) { return error.message; } })(),
    };
  });
  check('③ splitArgs 多余空格折叠、引号内空格不切',
    JSON.stringify(helpers.args) === JSON.stringify(['-y', '@x/y', 'D:\\Program Files\\z']),
    JSON.stringify(helpers.args));
  check('③ splitArgs 保留空引号参数', JSON.stringify(helpers.quotedEmpty) === JSON.stringify(['a', '', 'b']),
    JSON.stringify(helpers.quotedEmpty));
  check('③ parseMcpJson 认 mcpServers 包一层并带超时',
    helpers.json.id === 's1' && helpers.json.command === 'npx'
    && JSON.stringify(helpers.json.args) === JSON.stringify(['-y', 'a'])
    && helpers.json.start_timeout_ms === 60000,
    JSON.stringify(helpers.json));
  check('③ parseMcpJson 认裸格式，args 传字符串也切分',
    helpers.plain.id === 's2' && JSON.stringify(helpers.plain.args) === JSON.stringify(['-y', 'a']),
    JSON.stringify(helpers.plain));
  check('③ 非法 JSON / 缺 command 都报出可读错误',
    helpers.badJson.includes('JSON') && helpers.badCommand.includes('command'),
    `${helpers.badJson} | ${helpers.badCommand}`);

  // ── ④ 行高恒定：注入 600 字错误后，行高与普通行一致且副标题被截断 ────────────
  const realPayload = await mcpPayload();
  const longErrorPayload = {
    servers: [
      ...(realPayload.servers || []),
      {
        id: 'smoke-long', command: 'npx', args: ['-y', 'x'], env_keys: ['A', 'B'],
        start_timeout_ms: 30000, status: 'error', error: LONG_ERROR, connected: false,
        tools: [], active_calls: 0, activity: 'idle', last_used_at: null,
      },
    ],
  };
  await page.route('**/api/mcp', (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify(longErrorPayload),
  }));
  // 切走再切回 = 触发 loadMcpServers()（顶栏轮询是 15 秒一次，等它太慢也不确定）
  await page.click('[data-settings-tab="connections"]');
  await sleep(200);
  await page.click('[data-settings-tab="mcp"]');
  await sleep(700);

  const geometry = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('#mcpList .mcp-row')];
    const pick = (id) => {
      const row = rows.find((item) => item.dataset.mcpId === id);
      if (!row) return null;
      const sub = row.querySelector('.mcp-row-sub');
      return {
        height: Math.round(row.getBoundingClientRect().height),
        subClient: sub ? Math.round(sub.clientWidth) : -1,
        subScroll: sub ? Math.round(sub.scrollWidth) : -1,
        buttonsInSub: sub ? sub.querySelectorAll('button').length : -1,
      };
    };
    return { long: pick('smoke-long'), normal: pick('smoke-form'), count: rows.length };
  });
  check('④ 长错误行已渲染', Boolean(geometry.long), JSON.stringify(geometry));
  check('④ 行高恒定（长错误与普通行同高，差 ≤2px）',
    Boolean(geometry.long && geometry.normal) && Math.abs(geometry.long.height - geometry.normal.height) <= 2,
    JSON.stringify(geometry));
  check('④ 行高不超过阈值 90px（旧版挤压问题的回归锁）',
    Boolean(geometry.long) && geometry.long.height <= 90, JSON.stringify(geometry.long));
  check('④ 副标题单行截断（scrollWidth > clientWidth）',
    Boolean(geometry.long) && geometry.long.subScroll > geometry.long.subClient,
    JSON.stringify(geometry.long));
  check('④ 操作按钮不在副标题行里（不会被错误文本挤扁）',
    Boolean(geometry.long) && geometry.long.buttonsInSub === 0, JSON.stringify(geometry.long));
  await page.screenshot({ path: path.join(SHOT_DIR, 'mcp_connectors_list.png') });

  // ── ⑤ 展开区：命令原文 / 未连接时不含幻想数据 ──────────────────────────────
  await page.click('#mcpList .mcp-row[data-mcp-id="smoke-form"] .mcp-row-head');
  await sleep(250);
  const expanded = await page.evaluate(() => {
    const row = document.querySelector('#mcpList .mcp-row[data-mcp-id="smoke-form"]');
    const body = row.querySelector('.mcp-row-body');
    return {
      open: row.dataset.open === '1',
      aria: row.querySelector('.mcp-row-head').getAttribute('aria-expanded'),
      bodyHidden: body.hidden,
      text: body.textContent || '',
      tools: body.querySelectorAll('.mcp-tool-list code').length,
      actions: [...body.querySelectorAll('[data-mcp-action]')].map((button) => button.dataset.mcpAction),
      subHidden: row.querySelector('.mcp-row-sub').hidden,
    };
  });
  check('⑤ 点行头展开（data-open=1 + aria-expanded=true）',
    expanded.open && expanded.aria === 'true' && !expanded.bodyHidden, JSON.stringify(expanded));
  check('⑤ 展开区含命令原文（带空格的参数没被切碎）',
    expanded.text.includes('D:\\Program Files\\z'), expanded.text.slice(0, 200));
  check('⑤ 展开区列出环境变量 KEY 与启动超时',
    expanded.text.includes('SMOKE_TOKEN') && expanded.text.includes('30 秒'), expanded.text.slice(0, 300));
  check('⑤ 未连接时没有工具 chips（不展示幻想数据）', expanded.tools === 0, String(expanded.tools));
  check('⑤ 展开区有 测试/重连/编辑/删除 四个操作',
    JSON.stringify(expanded.actions) === JSON.stringify(['test', 'reconnect', 'edit', 'remove']),
    JSON.stringify(expanded.actions));
  check('⑤ 展开时副标题让位（不重复同一句话）', expanded.subHidden === true);
  await page.screenshot({ path: path.join(SHOT_DIR, 'mcp_connectors_expanded.png') });

  // 折叠回去
  await page.click('#mcpList .mcp-row[data-mcp-id="smoke-form"] .mcp-row-head');
  await sleep(200);
  check('⑤ 再点一次收起', await page.evaluate(() => document
    .querySelector('#mcpList .mcp-row[data-mcp-id="smoke-form"]').dataset.open === '0'));

  // ── ⑥ 搜索：过滤行；设置搜索能命中新导航项 ────────────────────────────────
  await page.fill('#mcpSearchInput', 'smoke-json');
  await sleep(300);
  const filtered = await page.evaluate(() => [...document.querySelectorAll('#mcpList .mcp-row')]
    .map((row) => row.dataset.mcpId));
  check('⑥ 列表搜索只剩命中的服务', JSON.stringify(filtered) === JSON.stringify(['smoke-json']), JSON.stringify(filtered));
  await page.fill('#mcpSearchInput', 'no-such-server');
  await sleep(300);
  check('⑥ 无命中时给出提示而不是空白', await page.evaluate(() => Boolean(document.querySelector('#mcpList .mcp-nomatch'))));
  await page.fill('#mcpSearchInput', '');
  await sleep(300);
  const countText = await page.textContent('#mcpCount');
  check('⑥ 计数显示「我的 MCP(n) · m 个已连接」', /我的 MCP\(\d+\)/.test(String(countText)), String(countText));

  await page.fill('#settingsSearch', 'MCP');
  await sleep(300);
  check('⑥ 设置搜索「MCP」命中新导航项',
    await page.evaluate(() => {
      const button = document.querySelector('[data-settings-tab="mcp"]');
      return Boolean(button) && !button.hidden;
    }));
  await page.fill('#settingsSearch', '');
  await sleep(200);

  // ── ⑦ 顶栏 MCP 圆点落到新分页 ────────────────────────────────────────────
  await page.unroute('**/api/mcp');
  await closeSettings();
  await page.click('#mcpStatus');
  await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
  await sleep(500);
  check('⑦ 顶栏 MCP 圆点点击落到「MCP 连接器」分页', await panelVisible('mcp'));
  check('⑦ 且不是旧的「局域网访问」分页', !(await panelVisible('connections')));

  // ── ⑧ 手机视口（375px）：不横向溢出 ──────────────────────────────────────
  await page.setViewportSize({ width: 375, height: 760 });
  await sleep(500);
  const mobile = await page.evaluate(() => {
    const content = document.querySelector('.settings-dialog .settings-content');
    const row = document.querySelector('#mcpList .mcp-row');
    return {
      docOverflow: document.documentElement.scrollWidth - window.innerWidth,
      contentOverflow: content ? content.scrollWidth - content.clientWidth : -1,
      rowWidth: row ? Math.round(row.getBoundingClientRect().width) : -1,
      contentWidth: content ? Math.round(content.clientWidth) : -1,
    };
  });
  check('⑧ 375px 下无横向溢出', mobile.docOverflow <= 1 && mobile.contentOverflow <= 1, JSON.stringify(mobile));
  check('⑧ 375px 下行宽不超过内容列', mobile.rowWidth > 0 && mobile.rowWidth <= mobile.contentWidth, JSON.stringify(mobile));
  await page.screenshot({ path: path.join(SHOT_DIR, 'mcp_connectors_mobile.png') });

  check('⑨ 全程无 JS 异常', pageErrors.length === 0, pageErrors.join(' | '));

  // ── 收尾：删掉探针建的两条，避免污染隔离实例（幂等，失败不致命）──────────
  await page.setViewportSize({ width: 1280, height: 900 });
  for (const id of ['smoke-form', 'smoke-json']) {
    await fetch(`${BASE}/api/mcp/remove`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server_id: id }),
    }).catch(() => {});
  }

  await browser.close();
  console.log(failures.length ? `\nFAILED (${failures.length}): ${failures.join(', ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('SMOKE CRASH:', error);
  process.exit(2);
});
