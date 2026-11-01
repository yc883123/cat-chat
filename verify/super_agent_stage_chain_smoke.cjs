// 真实 Super-agent 阶段链浏览器门禁：Writer/Novel、推进、retry、Approval、cursor、桌面/移动。
// 目标服务由 verify/_serve_super_agent_stage_chain.py 提供，页面和 HTTP 路由仍是真实源码。
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const PORT = Number(process.env.NAIBA_STAGE_CHAIN_PORT || 8819);
const BASE = process.env.NAIBA_STAGE_CHAIN_BASE || `http://127.0.0.1:${PORT}`;
const SERVER_ROOT = process.env.NAIBA_STAGE_CHAIN_ROOT || path.join(__dirname, '_tmp_stage_chain');
const FIXTURE = process.env.NAIBA_STAGE_CHAIN_FIXTURE || path.join(SERVER_ROOT, 'fixture.json');
const SHOTS = process.env.NAIBA_STAGE_CHAIN_SHOTS || path.join(__dirname, '_stage_chain_shots');
const PYTHON = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const failures = [];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${!ok && detail ? `  -> ${detail}` : ''}`);
  if (!ok) failures.push(label);
}
async function waitForServer() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try { const response = await fetch(BASE); if (response.status < 500) return; } catch {}
    await sleep(250);
  }
  throw new Error('阶段链隔离服务未就绪');
}
async function startServer() {
  fs.rmSync(SERVER_ROOT, { recursive: true, force: true });
  const child = spawn(PYTHON, [path.join(__dirname, '_serve_super_agent_stage_chain.py')], {
    env: { ...process.env, NAIBA_STAGE_CHAIN_PORT: String(PORT), NAIBA_STAGE_CHAIN_ROOT: SERVER_ROOT, NAIBA_STAGE_CHAIN_FIXTURE: FIXTURE, PYTHONIOENCODING: 'utf-8' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', (data) => { output += String(data); });
  child.stderr.on('data', (data) => { output += String(data); });
  try { await waitForServer(); return { child, output: () => output }; }
  catch (error) { child.kill(); throw new Error(`${error.message}\n${output.slice(-3000)}`); }
}
async function stopServer(child) {
  if (!child || child.killed) return;
  child.kill();
  await new Promise((resolve) => { child.once('exit', resolve); setTimeout(resolve, 3000); });
}
async function jsonRequest(method, url, body) {
  const response = await fetch(`${BASE}${url}`, {
    method, headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(`${method} ${url} -> ${response.status}: ${JSON.stringify(payload)}`);
  return payload;
}
function writerBody(bookId, chainId, modelName = 'probe') {
  return {
    domain: 'writer', book_id: bookId, chain: 'writer_v1', chain_version: 1,
    chain_instance_id: chainId, title: `Writer ${chainId}`, model_name: modelName,
    model_key: 'online:probe', stage_requests: { setup: {}, outline: {}, chapter_plan: {}, chapter_content: {} },
  };
}
function novelBody(bookId, chainId, modelName = 'probe') {
  return {
    domain: 'novel', book_id: bookId, chain: 'novel_v1', chain_version: 1,
    chain_instance_id: chainId, title: `Novel ${chainId}`, model_name: modelName,
    stage_requests: { import: {}, elements: { chapter_ids: [1] }, outline: { chapter_ids: [1] }, screenplay: { chapter_ids: [1] }, review: {} },
  };
}
async function createChain(body) { return jsonRequest('POST', '/api/super-agent/threads', body); }
async function action(threadId, nodeId, name, requestId, extra = {}) {
  return jsonRequest('POST', `/api/super-agent/threads/${encodeURIComponent(threadId)}/actions`, { action: name, node_id: nodeId, request_id: requestId, ...extra });
}
async function events(threadId, after = 0) { return jsonRequest('GET', `/api/super-agent/threads/${encodeURIComponent(threadId)}/events?after=${after}`); }

async function run() {
  fs.mkdirSync(SHOTS, { recursive: true });
  const server = await startServer();
  try {
    const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    const writer = await createChain(writerBody(fixture.writer_book_id, 'browser-writer-success'));
    const writerNodes = writer.nodes;
    check('Writer 创建返回完整四节点', writerNodes.length === 4);
    check('Writer 首节点 ready、后续 blocked', writerNodes[0].status === 'ready' && writerNodes.slice(1).every((node) => node.status === 'blocked'));
    const started = await action(writer.thread.thread_id, writerNodes[0].node_id, 'start', 'browser-writer-start');
    check('Writer start 后 running', started.result.node_status === 'running');
    // Fake writer task is done; sync drives the real Writer adapter/pipeline and promotion.
    let writerAfter;
    for (let attempt = 0; attempt < 12; attempt += 1) {
      await action(writer.thread.thread_id, writerNodes[0].node_id, 'sync', `browser-writer-sync-${attempt}`);
      writerAfter = (await jsonRequest('GET', `/api/super-agent/threads/${writer.thread.thread_id}/nodes`)).nodes;
      if (writerAfter[0].status !== 'running') break;
    }
    check('Writer 成功后第二节点 ready', writerAfter[0].status === 'succeeded' && writerAfter[1].status === 'ready');
    const cursor = await events(writer.thread.thread_id, 0);
    const promoted = cursor.events.filter((event) => event.event_type === 'stage_promoted');
    check('Writer 有 stage_promoted 事件', promoted.length === 1);
    const delta = await events(writer.thread.thread_id, cursor.last_sequence);
    check('事件 after cursor 只返回新增事件', delta.events.length === 0 && delta.last_sequence === cursor.last_sequence);

    const writerFail = await createChain(writerBody(fixture.retry_writer_book_id, 'browser-writer-retry', 'smoke-fail'));
    const failFirst = await action(writerFail.thread.thread_id, writerFail.nodes[0].node_id, 'start', 'browser-writer-fail-start');
    check('Writer 故意失败任务已启动', failFirst.result.node_status === 'running');
    await action(writerFail.thread.thread_id, writerFail.nodes[0].node_id, 'sync', 'browser-writer-fail-sync');
    let failNode = (await jsonRequest('GET', `/api/super-agent/threads/${writerFail.thread.thread_id}/nodes`)).nodes[0];
    check('Writer 失败后节点 failed', failNode.status === 'failed');
    const retried = await action(writerFail.thread.thread_id, failNode.node_id, 'retry', 'browser-writer-retry');
    check('Writer retry 递增 attempt', retried.result.attempt === 1 && retried.result.node_status === 'running');

    const novel = await createChain(novelBody(fixture.novel_book_id, 'browser-novel-approve'));
    check('Novel 创建返回五节点', novel.nodes.length === 5);
    check('Novel import 是前置 gate 且首节点 ready', novel.nodes[0].stage_key === 'import' && novel.nodes[0].status === 'ready');
    const imported = await action(novel.thread.thread_id, novel.nodes[0].node_id, 'start', 'browser-novel-import');
    check('Novel import 不提交 Studio 任务并直接完成', imported.result.node_status === 'succeeded' && imported.result.executor_id === '');
    const novelNodes = (await jsonRequest('GET', `/api/super-agent/threads/${novel.thread.thread_id}/nodes`)).nodes;
    const elements = novelNodes[1];
    const elementStart = await action(novel.thread.thread_id, elements.node_id, 'start', 'browser-novel-elements-start');
    await action(novel.thread.thread_id, elements.node_id, 'sync', 'browser-novel-elements-sync');
    const approvals = await jsonRequest('GET', `/api/super-agent/threads/${novel.thread.thread_id}/approvals`);
    const approval = approvals.approvals[0];
    check('Novel elements 完成后等待 Approval', approval && approval.status === 'pending');
    await jsonRequest('POST', `/api/super-agent/threads/${novel.thread.thread_id}/approvals/${approval.approval_id}`, { status: 'approved', request_id: 'browser-novel-approve' });
    const novelAfter = (await jsonRequest('GET', `/api/super-agent/threads/${novel.thread.thread_id}/nodes`)).nodes;
    check('Novel approve/apply 后下一阶段 ready', novelAfter[1].status === 'succeeded' && novelAfter[2].status === 'ready');
    check('Novel screenplay 对外键不泄漏 storyboard', novelAfter[3].stage_key === 'screenplay' && novelAfter[3].domain_ref.stage === 'script');

    const novelReject = await createChain(novelBody(fixture.novel_book_id, 'browser-novel-reject'));
    await action(novelReject.thread.thread_id, novelReject.nodes[0].node_id, 'start', 'browser-novel-reject-import');
    const rejectNodes = (await jsonRequest('GET', `/api/super-agent/threads/${novelReject.thread.thread_id}/nodes`)).nodes;
    const rejectElements = rejectNodes[1];
    const rejectStart = await action(novelReject.thread.thread_id, rejectElements.node_id, 'start', 'browser-novel-reject-start');
    await action(novelReject.thread.thread_id, rejectElements.node_id, 'sync', 'browser-novel-reject-sync');
    const rejectApproval = (await jsonRequest('GET', `/api/super-agent/threads/${novelReject.thread.thread_id}/approvals`)).approvals[0];
    await jsonRequest('POST', `/api/super-agent/threads/${novelReject.thread.thread_id}/approvals/${rejectApproval.approval_id}`, { status: 'rejected', request_id: 'browser-novel-reject' });
    const rejectAfter = (await jsonRequest('GET', `/api/super-agent/threads/${novelReject.thread.thread_id}/nodes`)).nodes;
    check('Novel reject 不推进后续节点', rejectAfter[1].status === 'failed' && rejectAfter[2].status === 'blocked');

    const browser = await chromium.launch({ channel: 'msedge', headless: true });
    try {
      for (const viewport of [{ name: 'desktop', width: 1280, height: 900 }, { name: 'mobile', width: 430, height: 900 }]) {
        const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } });
        const pageErrors = [];
        const consoleErrors = [];
        const badResponses = [];
        page.on('pageerror', (error) => pageErrors.push(String(error?.message || error)));
        page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()); });
        page.on('response', (response) => { if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) badResponses.push(`${response.status()} ${response.url()}`); });
        await page.route('**/favicon.ico', (route) => route.fulfill({ status: 204, body: '' }));
        const checkNoScriptError = () => {
          if (pageErrors.length || consoleErrors.length) throw new Error([...pageErrors, ...consoleErrors].join(' | '));
        };
        try {
          await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
          await page.waitForTimeout(700);
          const onboarding = page.locator('#onboardingDialog[open]');
          if (await onboarding.count()) await page.locator('#onboardingSkip').click();
          if (viewport.name === 'mobile') await page.locator('#openSidebar').click();
          await page.locator('#openSuperAgent').click();
          await page.waitForSelector('#superAgentView:not([hidden])', { timeout: 15000 });
          await page.locator('#superAgentCreate').click();
          await page.waitForSelector('#superAgentCreateDialog[open]', { timeout: 5000 });
          check(`${viewport.name}: 供应商下拉存在`, await page.locator('#superAgentCreateProvider').count() === 1);
          check(`${viewport.name}: 模型下拉存在`, await page.locator('#superAgentCreateModel').count() === 1);
          await page.waitForFunction(() => document.querySelector('#superAgentCreateModel')?.value === 'probe');
          checkNoScriptError();
          check(`${viewport.name}: 全局默认模型回填而非目录首项`, await page.locator('#superAgentCreateModel').inputValue() === 'probe');
          await page.locator('#superAgentCreateProvider').selectOption('online:probe-alt');
          await page.waitForFunction(() => document.querySelector('#superAgentCreateModel')?.value === 'alternate');
          check(`${viewport.name}: 供应商切换更新模型目录`, await page.locator('#superAgentCreateModel option[value="probe"]').count() === 0);
          await page.locator('#superAgentCreateProvider').selectOption('online:probe');
          await page.waitForFunction(() => document.querySelector('#superAgentCreateModel')?.value === 'probe');
          await page.locator('#superAgentCreateBookId').fill(fixture.writer_book_id);
          const hint = page.locator('#superAgentCreateModelHint');
          await page.route('**/api/providers/models', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ models: [] }) }));
          await page.locator('#superAgentCreateModelRefresh').click();
          await hint.filter({ hasText: '暂无可用模型' }).waitFor();
          await page.locator('#superAgentCreateSubmit').click();
          check(`${viewport.name}: 空目录阻止创建且有提示`, await page.locator('#superAgentCreateDialog').isVisible() && (await page.locator('#superAgentCreateError').innerText()).includes('可用模型'));
          await page.unroute('**/api/providers/models');
          await page.route('**/api/providers/models', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ error: '目录测试失败' }) }));
          await page.locator('#superAgentCreateModelRefresh').click();
          await hint.filter({ hasText: '模型目录读取失败' }).waitFor();
          check(`${viewport.name}: 目录失败有可见提示`, await hint.isVisible());
          await page.unroute('**/api/providers/models');
          await page.locator('#superAgentCreateModelRefresh').click();
          await page.waitForFunction(() => document.querySelector('#superAgentCreateModel')?.value === 'probe');
          check(`${viewport.name}: 目录失败后重试恢复`, (await hint.innerText()).includes('全局默认模型'));
          checkNoScriptError();
          const created = [];
          for (const [domain, bookId] of [['writer', fixture.writer_book_id], ['novel', fixture.novel_book_id]]) {
            if (domain === 'novel') await page.locator('#superAgentCreate').click();
            await page.locator('#superAgentCreateDomain').selectOption(domain);
            await page.locator('#superAgentCreateBookId').fill(bookId);
            await page.waitForFunction(() => document.querySelector('#superAgentCreateModel')?.value === 'probe');
            const responsePromise = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/super-agent/threads');
            await page.locator('#superAgentCreateSubmit').click();
            const response = await responsePromise;
            const submitted = response.request().postDataJSON();
            const payload = await response.json();
            check(`${viewport.name}: ${domain} 真实HTTP创建成功`, response.status() === 201 && !!payload.node?.node_id);
            check(`${viewport.name}: ${domain} 创建携带模型快照`, submitted.provider_id === 'probe' && submitted.model_key === 'online:probe' && submitted.model_name === 'probe');
            check(`${viewport.name}: ${domain} DTO不含checkpoint/raw`, !('checkpoint' in payload.node) && !('raw' in payload.node));
            created.push(payload.thread.thread_id);
            await page.locator('#superAgentCreateDialog').waitFor({ state: 'hidden' });
            checkNoScriptError();
          }
          await page.locator('#superAgentCreate').click();
          const dialogGeometry = await page.locator('#superAgentCreateDialog').evaluate((el) => ({ left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right, width: window.innerWidth, overflow: el.scrollWidth > el.clientWidth + 1 }));
          check(`${viewport.name}: 创建弹窗无横向溢出`, dialogGeometry.left >= 0 && dialogGeometry.right <= dialogGeometry.width + 1 && !dialogGeometry.overflow);
          await page.screenshot({ path: path.join(SHOTS, `super-agent-model-create-${viewport.name}.png`) });
          await page.locator('#superAgentCreateClose').click();
          // Refresh ensures the page consumes the server-created chain through real read DTOs.
          await page.locator('#superAgentRefresh').click();
          await page.locator(`[data-super-thread="${writer.thread.thread_id}"]`).click();
          await page.waitForFunction(() => document.querySelectorAll('#superAgentStageChain article.super-agent-stage').length === 4);
          check(`${viewport.name}: 阶段链真实投影可见`, await page.locator('#superAgentStageChain article.super-agent-stage').count() >= 4);
          check(`${viewport.name}: current 节点高亮`, await page.locator('#superAgentStageChain article.is-current').count() >= 1);
          const geometry = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
          check(`${viewport.name}: 无横向溢出`, geometry.scrollWidth <= geometry.clientWidth + 1, JSON.stringify(geometry));
          await page.screenshot({ path: path.join(SHOTS, `super-agent-stage-chain-${viewport.name}.png`) });
          await page.route('**/api/bootstrap', async (route) => {
            const response = await route.fetch();
            const payload = await response.json();
            payload.providers = []; payload.model_profiles = []; payload.default_model_key = '';
            await route.fulfill({ response, json: payload });
          });
          await page.reload({ waitUntil: 'domcontentloaded' });
          const welcome = page.locator('#onboardingDialog[open]');
          if (await welcome.count()) await page.locator('#onboardingSkip').click();
          if (viewport.name === 'mobile') await page.locator('#openSidebar').click();
          await page.locator('#openSuperAgent').click();
          await page.locator('#superAgentCreate').click();
          await page.locator('#superAgentCreateSubmit').click();
          check(`${viewport.name}: 无供应商阻止创建并引导设置`, !(await page.locator('#superAgentCreateProvider').isEnabled()) && (await page.locator('#superAgentCreateError').innerText()).includes('设置'));
          await page.unroute('**/api/bootstrap');
          checkNoScriptError();
        } catch (error) {
          check(`${viewport.name}: 浏览器阶段链冒烟`, false, String(error?.message || error).split('\n')[0]);
        } finally {
          check(`${viewport.name}: 零 pageerror`, pageErrors.length === 0, pageErrors.join(' | '));
          check(`${viewport.name}: 零 console.error`, consoleErrors.length === 0, consoleErrors.join(' | '));
          check(`${viewport.name}: 无 4xx/5xx`, badResponses.length === 0, badResponses.join(' | '));
          await page.close();
        }
      }
    } finally { await browser.close(); }
  } finally { await stopServer(server.child); }
  console.log(`\n结果：${failures.length ? `失败 ${failures.length} 项：${failures.join(' / ')}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
}
run().catch((error) => { console.error(error.stack || error); process.exit(1); });
