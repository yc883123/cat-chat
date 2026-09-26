// 教程正文截图驱动：对「冻结版隔离实例」跑 Playwright，拍出可发布的原始图。
//
// 用法：
//   node verify/_tutorial_shots.cjs <tutorial-id>     # 拍某一篇
//   node verify/_tutorial_shots.cjs recon             # 只拍空态，用于校准
//
// 前置：隔离实例已由 verify/_tutorial_serve.py 起在 NAIBA_TUT_BASE（默认 8799）。
// 口径见 skill `frozen-build-shot-harness`：冻结版 + 隔离数据根 + 公开品牌名供应商。
//
// 脱敏硬约束（拍完必须逐张复核）：
//   · 顶栏 API 只能是公开品牌名（DeepSeek 等），不得出现 teds / GG公益 / 摆烂白嫖 之流
//   · 侧栏工作区 / 会话标题不得出现真实工作区名（素材4 / naiba-chat）与真实会话标题
//   · 不得出现本地敏感路径、真实 Key
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const BASE = process.env.NAIBA_TUT_BASE || 'http://127.0.0.1:8799';
const OUT_ROOT = path.join(__dirname, '_tut_raw');
const TUT = (process.argv[2] || 'recon').trim();
// 模型兜底以 _tut_run.sh 导出的 NAIBA_TUT_MODEL 为准。别再写死 DeepSeek-V4-Pro——
// 那个 Key 已 HTTP 402，写死会让下一篇卡在「模型列表里找不到」上白等一轮。
const DEFAULT_MODEL = process.env.NAIBA_TUT_MODEL || 'mimo';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let pageErrors = [];

function outDir(tut) {
  const d = path.join(OUT_ROOT, tut);
  fs.mkdirSync(d, { recursive: true });
  return d;
}

// ── 基础工具 ────────────────────────────────────────────────────────────────

// 首启引导弹层会挡点击：close() 不够，要摘节点。
async function stripOverlays(page) {
  await page.evaluate(() => {
    document.getElementById('onboardingDialog')?.remove();
    document.getElementById('authDialog')?.remove();
  });
}

// 底部「模型」是自定义面板（隐藏原生 select + #composerModelList 按钮列表）。
// 不选它发送键就是 disabled —— 每个会话开场都得先选。
async function selectComposerModel(page, labelFragment) {
  await page.click('#composerModelTrigger');
  await page.waitForSelector('#composerModelPanel:not([hidden])', { timeout: 8000 });
  await sleep(400);
  const labels = await page.locator('#composerModelList .composer-model-option')
    .allInnerTexts();
  // 大小写不敏感地找：显示名由后端模型名派生（如 deepseek-v4-pro → 「DeepSeek-V4-Pro」），
  // 供应商换一个名字，大小写就可能对不上——硬比 hasText 会静默找不到。
  const needle = String(labelFragment || '').toLowerCase();
  const hit = labels.findIndex((t) => t.toLowerCase().includes(needle));
  if (hit < 0) {
    throw new Error(
      `模型列表里找不到「${labelFragment}」。当前可选：${labels.map((t) => t.trim()).join(' | ') || '（空）'}`,
    );
  }
  await page.locator('#composerModelList .composer-model-option').nth(hit).click();
  await sleep(600);
  const text = await page.locator('#composerModelTriggerText').innerText();
  if (!text || text.includes('请选择')) throw new Error(`模型没选上，触发器仍显示「${text}」`);
  console.log(`  model -> ${text.trim()}`);
  return text.trim();
}

// 侧栏分组默认折叠：把未展开的都点开（虚拟化列表，点完要等重渲染）
async function expandGroups(page) {
  const groups = page.locator('#sidebarWorkspaceTree .workspace-group');
  // 全新实例树里只有「暂无对话 ＋ 新建会话」，没有分组。别用 waitForSelector 硬等——
  // 它会白等满超时（20s × 每篇），先探一下再决定要不要等。
  if (await groups.count() === 0) {
    await sleep(500);
    if (await groups.count() === 0) return;
  }
  for (let i = 0; i < 10; i += 1) {
    const collapsed = page.locator('#sidebarWorkspaceTree .workspace-group:not(.expanded) .workspace-group-header');
    if (await collapsed.count() === 0) break;
    await collapsed.first().click();
    await sleep(300);
  }
}

// 新开一个会话。注意：**会话一旦发出首条消息，Agent 下拉就被锁死**
//（工具集在首条消息时固化，UI 提示「会话内不可切换」）——所以「换 Agent」必须先新开会话，
// 且在发首条消息之前选好。新会话的模型也要重选（模型是会话级）。
//
// 入口有两个：会话树为空时是 `[data-action="new-chat"]`（「＋ 新建会话」），
// 有会话之后变成分组里的 `[data-action="new-in-group"]`（「＋ 新会话」）。
async function newConversation(page, modelFragment) {
  await expandGroups(page);
  const direct = page.locator('#sidebarWorkspaceTree [data-action="new-chat"]');
  const inGroup = page.locator('#sidebarWorkspaceTree [data-action="new-in-group"]');
  if (await direct.count()) {
    await direct.first().click();
  } else if (await inGroup.count()) {
    await inGroup.first().click();
  } else {
    throw new Error('侧栏里找不到新建会话入口');
  }
  await sleep(1200);
  await stripOverlays(page);
  await page.waitForSelector('#emptyState', { timeout: 15000 });
  await sleep(500);
  if (modelFragment !== false) {
    await selectComposerModel(page, modelFragment || DEFAULT_MODEL);
  }
}

async function newPage(browser, { viewport = { width: 1440, height: 900 } } = {}) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 2 });
  page.on('pageerror', (e) => pageErrors.push(`pageerror: ${e.message}`));
  await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 30000 });
  await stripOverlays(page);
  await page.waitForSelector('#emptyState', { timeout: 20000 });
  await sleep(600);
  // 一律开一条干净的会话再拍。理由有两层：
  //  ① app 载入时打开的是「最新那条会话」，它可能已固化工具集 ⇒ Agent 下拉锁死；
  //  ② 全新实例**没有会话**时 `#agentSelect` 看着可用，但 `saveAgentSelection()`
  //     一开头就 `if (!state.conversationId) return`——切 Agent 只弹个 toast 什么也不做。
  // 两种情况都会让后面的切 Agent 静默失效，统一在起点消掉。
  await newConversation(page, false);
  await selectComposerModel(page, DEFAULT_MODEL);
  return page;
}

async function shot(page, tut, name) {
  const p = path.join(outDir(tut), name);
  await page.screenshot({ path: p });
  console.log('  shot', name);
  return p;
}

// 把页面上出现的**真实密钥**就地替换成等长的 `xxxx`，再截图。
//
// 教程 3 绕不过这一步：正文第 2 步就是「把 Key 发给 AI」，那条用户消息里躺着真 Key，
// 而图是要进公开仓库的。做法是在 DOM 文本节点上原地替换（不是打码、不是裁掉），
// 替换后与正文示例里的 `xxxxxxxxxxxxxxxx` 观感完全一致，读者看到的就是他该看到的样子。
// 只处理文本节点——输入框的 value 不走 textContent，但消息发出后输入框已被清空，不构成遗漏。
async function redactText(page, secret) {
  const needle = String(secret || '').trim();
  if (!needle) return 0;
  const hits = await page.evaluate((s) => {
    const masked = 'x'.repeat(s.length);
    let count = 0;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const text = node.nodeValue || '';
      if (!text.includes(s)) continue;
      count += text.split(s).length - 1;
      node.nodeValue = text.split(s).join(masked);
    }
    return count;
  }, needle);
  console.log(`  脱敏：替换 ${hits} 处密钥字面量（掩码 ${'x'.repeat(needle.length)}）`);
  if (hits === 0) {
    // 一处都没替换到，要么密钥压根没上屏、要么被拆进了多个节点。
    // **不能静默通过**：真发生了就是密钥直接进图，必须让人看见。
    console.log('  ⚠ 没在 DOM 里找到密钥字面量（若它确实上屏了，就要人工复核这张图）');
  }
  return hits;
}

// 技能的 Site Selection 会先问「AI 站 / CN 站」。本机 `~/.openclaw/openclaw.json` 里
// `skills.entries.runninghub.site` 已经是 `ai`，脚本会自己读；但**模型不知道**，仍可能弹一次问。
// 弹了就点「AI 站」，不弹什么都不做——这步不在教程正文里，别让它卡住拍摄。
async function answerSiteQuestionIfAsked(page) {
  const host = page.locator('#choiceButtons');
  if (await host.count() === 0) return false;
  const text = (await host.innerText().catch(() => '')) || '';
  if (!/站点|runninghub\.(ai|cn)|国际站|中文站/i.test(text)) return false;
  const options = host.locator('button');
  const total = await options.count();
  for (let i = 0; i < total; i += 1) {
    const label = ((await options.nth(i).innerText().catch(() => '')) || '').trim();
    if (/AI\s*站|国际站/.test(label)) {
      console.log(`  站点选择：点「${label}」`);
      await options.nth(i).click();
      await waitRunDone(page, 300000);
      return true;
    }
  }
  console.log('  站点选择面板里没找到「AI 站」选项，跳过');
  return false;
}


// 原生 select 里可能混着伪选项（形如 __refresh_xxx__），一律按「value 非空且非 __ 前缀」挑，
// 绝不按下标——选到伪选项会静默不生效。
async function pickOptionValue(page, selector, matchText) {
  return page.evaluate(({ sel, txt }) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const ok = (o) => o.value && !String(o.value).startsWith('__');
    const opt = [...el.options].find((o) => ok(o) && (o.textContent || '').includes(txt))
      || [...el.options].find((o) => ok(o) && (o.value || '').includes(txt));
    return opt ? opt.value : null;
  }, { sel: selector, txt: matchText });
}

async function selectAgent(page, nameFragment) {
  // 会话一旦发过首条消息，工具集就固化了，`#agentSelect` 变成 disabled
  //（`07-models-agents.js:959` locked = enabled_tool_ids 非空）。
  // 直接 selectOption 会卡满 30s 超时才报错，先断言给一句能看懂的错。
  if (await page.locator('#agentSelect').isDisabled()) {
    throw new Error('Agent 下拉被锁：当前会话已固化工具集，必须先新开会话再切 Agent');
  }
  const val = await pickOptionValue(page, '#agentSelect', nameFragment);
  if (!val) throw new Error(`Agent 下拉里找不到「${nameFragment}」`);
  await page.selectOption('#agentSelect', val);
  await sleep(800);
  const now = await page.locator('#agentSelect').inputValue();
  if (now !== val) throw new Error(`Agent 没切过去：期望 ${val}，实际 ${now}`);
  const composer = await page.inputValue('#messageInput');
  console.log(`  agent -> ${nameFragment}｜输入框预填: ${JSON.stringify(composer.slice(0, 60))}`);
  return val;
}

async function openSettings(page, tab) {
  await page.click('#openSettings');
  await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
  await sleep(500);
  if (tab) {
    await page.click(`[data-settings-tab="${tab}"]`);
    await sleep(700);
  }
}

async function closeSettings(page) {
  await page.keyboard.press('Escape');
  await sleep(500);
}

// 审批模式是**会话级**的：`08-conversations.js:866` 把新建会话硬编码成 'auto'，
// 而全局配置 permission_mode 是 'confirm' —— 两者不一致（已在报告里单独提）。
// 要拍「工具确认框」就必须显式切到 confirm，否则有副作用的工具会直接执行、不弹卡。
async function setPermissionMode(page, mode) {
  const label = { confirm: '确认', auto: '自动', full: '完全' }[mode] || mode;
  await page.click('#permissionModeButton');
  await page.waitForSelector('#permissionModeMenu:not([hidden])', { timeout: 8000 });
  await sleep(300);
  await page.click(`#permissionModeMenu [data-permission-mode="${mode}"]`);
  await sleep(700);
  const val = (await page.locator('#permissionModeValue').innerText()).trim();
  if (val !== label) throw new Error(`审批模式没切到「${label}」，当前是「${val}」`);
  console.log(`  审批 -> ${val}`);
}

// 发送键在「没选模型」时是 disabled——先断言前提成立再点，避免静默无效。
//
// ⚠️ 不能整体覆盖输入框：切 Agent 时 `saveAgentSelection()` 会调
// `appendPresetSkillsToComposer()` 把该 Agent 的固定 Skill 以 `/ref` 预填进输入框
// （`07-models-agents.js:982`）。首轮的**冻结技能集**正是由这些引用决定的
// （`naiba/run/chat.py:321-336`：referenced_ids → skill_policy.skill_ids）。
// 用 fill 一把覆盖 ⇒ referenced_ids 为空 ⇒ 教程助手冻结不到 cat-chat-guide
// ⇒ 它会答「我这边没查到 Cat Chat 的说明书」。所以必须**追加**，模拟真人往框里接着打字。
async function typeMessage(page, text) {
  const existing = (await page.inputValue('#messageInput')).trimEnd();
  if (existing) console.log(`  composer 已有预填引用，追加：${existing.slice(0, 40)}`);
  await page.fill('#messageInput', existing ? `${existing} ${text}` : text);
  await sleep(400);
}

async function sendMessage(page, text) {
  await typeMessage(page, text);
  const disabled = await page.locator('#sendButton').isDisabled();
  if (disabled) throw new Error('发送键是 disabled：模型没选上？');
  await page.click('#sendButton');
  console.log(`  send -> ${text.slice(0, 28)}${text.length > 28 ? '…' : ''}`);
}

// 完成信号用可见状态类 is-running 的「出现→消失」，不要固定 sleep 硬等。
//
// 默认**自动放行**途中的工具确认卡。理由：AI 会自己去搜工作区的父目录/其它路径，
// app 的越界闸门就会弹卡——真用户会点「允许」，harness 不点就只能硬等到超时
//（`probe-tutor` 假失败的真因：截图停在「读取工作区外路径」那张卡上）。
// 需要**确认卡本体入镜**的篇（tutorial-01/04-approve、tutorial-06/03-approve）
// 不走这里——它们自己 `waitForSelector('.tool-confirm')` 再截图。
async function waitRunDone(page, timeoutMs = 240000, { autoApprove = true } = {}) {
  const deadline = Date.now() + timeoutMs;
  await page.waitForFunction(
    () => document.getElementById('composerForm')?.classList.contains('is-running'),
    null, { timeout: 20000 },
  ).catch(() => { /* 极快的回复可能一闪而过 */ });
  let approved = 0;
  while (Date.now() < deadline) {
    if (autoApprove) {
      const approve = page.locator('.tool-confirm .tool-confirm-approve');
      if (await approve.count() > 0 && await approve.first().isVisible().catch(() => false)) {
        await approve.first().click().catch(() => {});
        approved += 1;
        await sleep(900);
        continue;
      }
    }
    const running = await page.evaluate(
      () => document.getElementById('composerForm')?.classList.contains('is-running'),
    );
    if (!running) break;
    await sleep(700);
  }
  if (approved) console.log(`  途中自动放行确认 ${approved} 次`);
  await sleep(900);
}

async function lastMessageText(page) {
  return page.evaluate(() => {
    const rows = [...document.querySelectorAll('#messages .message-row[data-message-id]')];
    return rows.length ? (rows[rows.length - 1].innerText || '').slice(0, 400) : '';
  });
}

// 等消息气泡里出现 ≥minCount 张生成图。
// 必须**在 run 结束之后**再等：`comfyui_batch` 默认 wait=false，工具当场就返回 job_id，
// AI 这一轮随即结束；图片是 Job 跑到终态后由 `JobRegistry._write_back_media` 异步挂回
// **发起它的那条助手消息**的（`naiba/jobs.py:296-311`）。所以「run 结束」不等于「图在了」。
async function waitForMessageMedia(page, minCount, timeoutMs = 900000) {
  const deadline = Date.now() + timeoutMs;
  let last = -1;
  while (Date.now() < deadline) {
    const n = await page.locator('#messages .media-grid .media-item').count();
    if (n !== last) { console.log(`  消息内生成图 -> ${n} 张（目标 ${minCount}）`); last = n; }
    if (n >= minCount) return n;
    await sleep(2000);
  }
  throw new Error(`等生成图超时：期望 ${minCount} 张，实际 ${last} 张`);
}

// 打 `/` 呼出技能索引浮层（自定义元素，不是原生 select，能截进图）
// `/` 呼出 Skill 索引浮层。给了 filter 就继续往下打字把它过滤到目标技能上
//（浮层按 `/` 之后到光标之间的那段文本过滤，见 `13-skill-refs.js` 的 skillTokenAt）。
async function openSlashPopup(page, filter) {
  await page.click('#messageInput');
  await page.fill('#messageInput', '');
  await sleep(200);
  await page.type('#messageInput', filter ? `/${filter}` : '/');
  await page.waitForSelector('#skillPopup:not([hidden])', { timeout: 8000 });
  await sleep(600);
  const n = await page.locator('#skillPopup .skill-popup-item').count();
  if (!n) {
    throw new Error(
      filter
        ? `\`/\` 浮层里没有匹配「${filter}」的 Skill（技能没随包进去？）`
        : '`/` 浮层打开了但一个 Skill 都没有',
    );
  }
  console.log(`  slash 浮层 -> ${n} 个 Skill${filter ? `（过滤「${filter}」）` : ''}`);
  return n;
}

// 设置 → 连接状态 → MCP 服务 面板。
// 先把设置关掉再开：调用方可能刚从 Agent 编辑器里出来，设置对话还开着，
// 再点一次 `#openSettings` 会把它关掉（同一个按钮 toggle）。
async function openMcpPanel(page) {
  await page.evaluate(() => document.getElementById('settingsDialog')?.close());
  await sleep(300);
  await openSettings(page, 'connections');
  const list = page.locator('#mcpList');
  await list.waitFor({ timeout: 8000 });
  const n = await list.locator('.connection-item').count();
  if (!n) {
    throw new Error(
      'MCP 列表是空的——隔离实例没播到演示注册项（_tut_run.sh 里 NAIBA_TUT_SEED_MCP=1？）',
    );
  }
  await list.scrollIntoViewIfNeeded().catch(() => {});
  await sleep(600);
  console.log(`  MCP 面板 -> ${n} 条注册项`);
  return n;
}

async function openTasksPanel(page) {
  await page.click('#openTasks');
  await page.waitForSelector('#tasksDialog[open]', { timeout: 10000 });
  await sleep(900);
}

async function closeDialog(page, id) {
  await page.evaluate((i) => document.getElementById(i)?.close(), id);
  await sleep(500);
}

// 设置 → Agent → 点开某张卡片 → 切到指定分区
async function openAgentEditor(page, nameFragment, tab) {
  await openSettings(page, 'agent');
  const card = page.locator('#agentCards [data-agent-card]').filter({ hasText: nameFragment }).first();
  await card.waitFor({ timeout: 10000 });
  await card.click();
  await page.waitForSelector('#agentDialog[open]', { timeout: 10000 });
  await sleep(800);
  if (tab) {
    await page.click(`#agentDialog [data-agent-tab="${tab}"]`);
    await sleep(700);
  }
}

// 隔离工作区里造一批「演示照片」，给教程 6 的批量重命名真跑用。
// 路径取中性名（`D:\tutorial-demo\workspace\照片`），不带任何真实个人目录。
function prepareDemoPhotos() {
  const root = process.env.NAIBA_TUT_ROOT || 'D:/tutorial-demo';
  const dir = path.join(root, 'workspace', '照片');
  fs.mkdirSync(dir, { recursive: true });
  const names = ['IMG_2031.jpg', 'IMG_2032.jpg', 'IMG_2033.jpg', 'IMG_2034.png', 'IMG_2035.jpg'];
  for (const name of names) {
    const target = path.join(dir, name);
    if (!fs.existsSync(target)) fs.writeFileSync(target, `demo ${name}\n`);
  }
  console.log(`  demo 照片目录 -> ${dir}（${names.length} 个文件）`);
  return dir;
}

// 隔离工作区里造一份**能真出图**的 ComfyUI API 格式工作流，给教程 2 真跑用。
// 必须在**隔离根重置之后**写（`_tut_run.sh` 每轮把整个根 `mv` 走再重新播种，
// 事先放在根里的文件会被搬走）——所以由驱动脚本自己落盘，而不是预置。
// seed=-1：`_normalize_comfyui_runtime_workflow` 会把负 seed 换成随机值，
// 于是 shots=6 的 6 次提交拿到 6 个不同种子（`naiba/tools/providers/comfyui.py:57-69`）。
// 画面刻意选**无人物**的产品静物：教程图要能对外发布，带人脸的真实产物图不能用。
function prepareComfyWorkflow() {
  const root = process.env.NAIBA_TUT_ROOT || 'D:/tutorial-demo';
  const dir = path.join(root, 'workspace');
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 'cover_api.json');
  const ckpt = process.env.NAIBA_TUT_CKPT || 'XL-epicrealismXL_vxviLastfameRealism.safetensors';
  const workflow = {
    4: { class_type: 'CheckpointLoaderSimple', inputs: { ckpt_name: ckpt } },
    6: {
      class_type: 'CLIPTextEncode',
      inputs: {
        text: 'minimal product photo of a white ceramic coffee cup on a walnut desk, '
            + 'soft window light, shallow depth of field, clean background, editorial style',
        clip: ['4', 1],
      },
    },
    7: {
      class_type: 'CLIPTextEncode',
      inputs: { text: 'text, watermark, logo, blurry, lowres, people, hands', clip: ['4', 1] },
    },
    5: { class_type: 'EmptyLatentImage', inputs: { width: 768, height: 1024, batch_size: 1 } },
    3: {
      class_type: 'KSampler',
      inputs: {
        seed: -1, steps: 20, cfg: 7, sampler_name: 'euler', scheduler: 'normal', denoise: 1,
        model: ['4', 0], positive: ['6', 0], negative: ['7', 0], latent_image: ['5', 0],
      },
    },
    8: { class_type: 'VAEDecode', inputs: { samples: ['3', 0], vae: ['4', 2] } },
    9: { class_type: 'SaveImage', inputs: { filename_prefix: 'cover', images: ['8', 0] } },
  };
  fs.writeFileSync(file, JSON.stringify(workflow, null, 2), 'utf8');
  console.log(`  ComfyUI 工作流 -> ${file}（ckpt=${ckpt}）`);
  return file;
}

// ── 各篇拍摄脚本 ────────────────────────────────────────────────────────────

const SHOTS = {
  // 取证探针：教程助手的知识库到底有没有被冻结进会话。
  // 盯三件事：① 切 Agent 前有没有会话（空态下 saveAgentSelection 直接 return，只弹 toast）；
  // ② 切完输入框里有没有 /ref 预填；③ 发送后会话的 skill_policy 是否非空。
  async 'probe-tutor'(page) {
    const dump = async (tag) => {
      const info = await page.evaluate(() => ({
        agentValue: document.getElementById('agentSelect')?.value,
        agentDisabled: document.getElementById('agentSelect')?.disabled,
        composer: document.getElementById('messageInput')?.value,
        convItems: document.querySelectorAll('#sidebarWorkspaceTree .conversation-item').length,
        emptyVisible: !document.getElementById('emptyState')?.hidden,
      }));
      console.log(`  [${tag}]`, JSON.stringify(info));
      return info;
    };
    await dump('load');
    // newPage 已经开好一条干净会话（0 条会话时切 Agent 是空操作，见 newPage 注释）
    await selectAgent(page, '教程助手');
    const after = await dump('after-agent');
    if (!/^\s*\/\S/.test(after.composer || '')) {
      throw new Error('切到教程助手后输入框里没有 /ref 预填 —— 知识库不会被冻结');
    }
    await sendMessage(page, '我不会换聊天背景图，在哪换？');
    await waitRunDone(page);
    await shot(page, 'probe-tutor', 'answer.png');
    const text = await lastMessageText(page);
    console.log('  ANSWER:', text.replace(/\s+/g, ' ').slice(0, 300));
  },

  // 取证探针：只为看 executor 的 [PERM] 判定日志（需 serve 侧开 NAIBA_DEBUG_PERMISSION=1）
  async 'probe-perm'(page) {
    await selectAgent(page, '全能');
    // 空态下「审批」按钮是 disabled —— 必须先有一条会话才可切档
    await sendMessage(page, '你好。');
    await waitRunDone(page);
    await setPermissionMode(page, 'confirm');
    // craft 模式下「工作区内 write_file/edit_file」被 CraftToolExecutor 直接放行，
    // 所以探针改用跑命令（pwsh）来触发确认。
    await sendMessage(page, '用命令看一下当前工作区目录里有哪些文件。');
    await waitRunDone(page);
    await shot(page, 'probe-perm', 'after.png');
  },

  // 校准用：只拍空态，看清侧栏 / 顶栏都显示什么
  async recon(page) {
    await shot(page, 'recon', '00-empty.png');
    const info = await page.evaluate(() => ({
      api: document.getElementById('modelSelect')?.selectedOptions?.[0]?.textContent?.trim(),
      agent: document.getElementById('agentSelect')?.selectedOptions?.[0]?.textContent?.trim(),
      skills: document.getElementById('skillCount')?.textContent?.trim(),
      workspaces: [...document.querySelectorAll('#sidebarWorkspaceTree .workspace-group-header')]
        .map((n) => n.innerText.trim().slice(0, 40)),
      conversations: [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item')]
        .map((n) => n.innerText.trim().slice(0, 40)),
      starterCards: [...document.querySelectorAll('#emptyState .starter-card, #emptyState button')]
        .map((n) => n.innerText.trim().split('\n')[0]).filter(Boolean).slice(0, 12),
      composerModelText: document.getElementById('composerModelTriggerText')?.textContent?.trim(),
      composerModelOptions: [...(document.getElementById('composerModelSelect')?.options || [])]
        .map((o) => `${o.value} | ${o.textContent.trim()}`).slice(0, 10),
      sendDisabled: document.getElementById('sendButton')?.disabled,
    }));
    console.log(JSON.stringify(info, null, 2));
  },

  // 教程 1 · 第一次对话
  async 'tutorial-01'(page) {
    // 01 配置在线 API：设置 → API 供应商 → 添加 API → 选中 DeepSeek 预设
    // （正文口径：「点一个平台预设模板，名称、地址、请求格式会自动填好，只需粘 Key」）
    await openSettings(page, 'models');
    await page.click('#providerCards [data-provider-add]');
    await page.waitForSelector('#providerDialog[open]', { timeout: 10000 });
    await sleep(600);
    await page.click('#providerPresetGrid [data-provider-preset="deepseek"]');
    await sleep(800);
    await shot(page, 'tutorial-01', '01-api.png');
    await page.keyboard.press('Escape');
    await sleep(400);
    await closeSettings(page);

    // 02 Agent 说明弹层
    await page.click('#agentHelpButton');
    await page.waitForSelector('#agentHelpPopover:not([hidden])', { timeout: 8000 });
    await sleep(700);
    await shot(page, 'tutorial-01', '02-agent-help.png');
    await page.keyboard.press('Escape');
    await sleep(400);

    // 03 第一次对话（真跑，话术照抄正文）
    await selectAgent(page, '全能');
    await sendMessage(page, '你好，用一句话介绍一下你自己，然后告诉我你能帮我做哪些事。');
    await waitRunDone(page);
    await shot(page, 'tutorial-01', '03-first-chat.png');

    // 04 工具确认框：craft 模式下「工作区内 write_file/edit_file」被 CraftToolExecutor
    // 直接放行（plans.py:124），所以写文件永远不弹卡。改用「跑命令」触发，且挑一条
    // 不带路径的命令，避免把隔离根路径带进图里。
    await setPermissionMode(page, 'confirm');
    await sendMessage(page, '帮我看一下我的电脑上装的是哪个版本的 Python。');
    await page.waitForSelector('.tool-confirm', { timeout: 150000 });
    await sleep(1000);
    await shot(page, 'tutorial-01', '04-approve.png');
    // 收尾：拒绝掉，别让它真跑（拒绝后 AI 还会补一句话，等它说完再走）
    await page.click('.tool-confirm .tool-confirm-reject').catch(() => {});
    await waitRunDone(page, 120000).catch(() => {});

    // 05 问教程助手（真跑，话术照抄正文）
    // Agent 在会话内不可切换 ⇒ 必须新开会话，并在发首条消息前选中教程助手。
    await newConversation(page);
    await selectAgent(page, '教程助手');
    await sendMessage(page, '我不会换聊天背景图，在哪换？');
    await waitRunDone(page);
    await shot(page, 'tutorial-01', '05-tutor.png');
  },

  // 教程 2 · ComfyUI 出图（本机真跑：真 ComfyUI 8188 + 真工作流 + 真出图）
  // 前提：`D:\tutorial-demo\workspace\cover_api.json` 已备好（SDXL API 格式，seed=-1 随机），
  // 且 ComfyUI 在 8188 运行中。工作流先单独 POST /prompt 验过能出图，再交给 AI 走一遍。
  async 'tutorial-02'(page) {
    // 工作流由驱动自己落盘：隔离根每轮被整个搬走重建，预置在根里的文件留不下来。
    prepareComfyWorkflow();

    // 01-home：全新会话的开始页。**必须在切 Agent 之前拍**——先切导演的话，
    // `appendPresetSkillsToComposer` 会把它的三个固定技能以 `/ref` 预填进输入框，
    // 还会弹一条「Agent 已切换」toast，正文要的「空态主界面」就被糊掉了。
    await shot(page, 'tutorial-02', '01-home.png');

    // 02-agent-switch：顶栏切到导演 Agent。
    // ⚠️ Agent 下拉是**原生 `<select>`**，弹层由系统绘制、不进页面截图——只能拍「已选中」的顶栏，
    // 拍不出「下拉里排第几个」。正文配图口径据此改（别再写「下拉里选中」）。
    await selectAgent(page, '导演');
    await shot(page, 'tutorial-02', '02-agent-switch.png');

    // 03-starter-card：Agent 是**会话级**的，新会话要重切；再点「通过 HTTP 调用 ComfyUI」卡
    // → 点卡即发（`sendChatMessage` 会把卡上的指令与输入框里已有的 `/ref` 预填合并）。
    await newConversation(page);
    await selectAgent(page, '导演');
    const card = page.locator('.starter-grid .custom-starter')
      .filter({ hasText: '通过 HTTP 调用 ComfyUI' }).first();
    await card.waitFor({ timeout: 10000 });
    await card.locator('button').first().click();
    console.log('  点预设卡「通过 HTTP 调用 ComfyUI」（自动发送）');
    await sleep(6000);   // 让「已发送 + AI 开始探测」的状态入镜
    await shot(page, 'tutorial-02', '03-starter-card.png');
    await waitRunDone(page, 300000);

    // 04-result：真出图。workflow_paths + shots 是 comfyui_batch 的口径
    //（`naiba/tools/providers/comfyui.py:96-110`：shots 对 workflow_paths 逐条重复提交）。
    await sendMessage(page, '用工作区里的 cover_api.json 出 6 张封面。');
    await waitRunDone(page, 600000);
    await waitForMessageMedia(page, 6);
    await shot(page, 'tutorial-02', '04-result.png');

    // 05-tasks：任务面板（批量出图在后台跑，不挡聊天）
    await openTasksPanel(page);
    await shot(page, 'tutorial-02', '05-tasks.png');
    await closeDialog(page, 'tasksDialog');

    // 06-agent-tools：设置 → Agent → 导演 → 工具集（正文「导演 Agent 开箱就带 18 件工具」）
    await openAgentEditor(page, '导演', 'tools');
    await shot(page, 'tutorial-02', '06-agent-tools.png');
    await closeDialog(page, 'agentDialog');

    // 07-mcp：设置 → 连接状态 → MCP 服务（正文「进阶（可选）：走 MCP 通道」的收口）
    // 为什么拍面板而不是真点「设置本地 Comfy MCP」卡：那张卡会让 AI 在**本机**跑
    // pip install（隔离实例之外的真实副作用），且结果取决于本机 ComfyUI 装在哪儿。
    // 面板是确定性画面：`renderMcp()` 只渲染「id · 状态」+ 一行明细，command/env 不进 DOM。
    await openMcpPanel(page);
    await shot(page, 'tutorial-02', '07-mcp.png');
    await closeSettings(page);
  },

  // 教程 3 · RunningHub 云端出图
  //
  // **前提是「能用标准模型 API 的 Key」**：2026-09-26 实测本人的个人版 Key（apiType=NORMAL）
  // 调任何模型端点都回 `errorCode 1014 Access Denied：标准模型API仅限企业级-共享API Key调用`，
  // 只有 AI 应用通道（`runninghub_app.py`）能用。所以：
  //   · `04-dialog`（模型菜单）**不需要真出图**，任何能鉴权的 Key 都能拍；
  //   · `03-key` 拍的是「把 Key 交给 AI 让它自验」，也只需要鉴权；
  //   · `05-result` 需要**真跑出图**，被 1014 / 余额挡住时就只能跳过，绝不假装成功。
  // Key 从环境变量读（`NAIBA_TUT_RH_KEY`），**不写进仓库**；截图前一律先 `redactText`。
  async 'tutorial-03'(page) {
    const key = (process.env.NAIBA_TUT_RH_KEY || '').trim();
    await selectAgent(page, '导演');
    await openSlashPopup(page, 'runninghub');
    await shot(page, 'tutorial-03', '02-slash-skill.png');
    await page.keyboard.press('Escape');
    await sleep(400);

    if (!key) {
      console.log('  未给 NAIBA_TUT_RH_KEY ⇒ 03-key / 04-dialog / 05-result 全部跳过');
      return;
    }

    // 03-key：正文话术原样发出去，让 AI 自己验、自己存
    await sendMessage(page, `这是我的 RunningHub API Key：${key}，帮我测试能不能用，然后存下来。`);
    await waitRunDone(page, 420000);
    // 技能会先问站点（`api-key-setup.md` / SKILL.md 的 Site Selection）——本机
    // `~/.openclaw/openclaw.json` 已写死 `site: ai`，但**模型不知道**，仍可能弹一次。
    // 弹了就按「AI 站」点掉，别让流程卡在这（这一步本身不是正文讲的内容）。
    await answerSiteQuestionIfAsked(page);
    await redactText(page, key);
    await shot(page, 'tutorial-03', '03-key.png');

    // 04-dialog：说要什么。按技能 RULE 7，出图前**必须先给固定 5 项模型菜单并等用户选**，
    // 所以这张拍的就是那个菜单（正文那句「先把要提交的参数整理成一张表念给你听」）。
    await sendMessage(page, '用 RunningHub 出一张古风人物立绘，竖版 1024×1536。');
    await waitRunDone(page, 420000);
    // 站点问也可能拖到这一步才出现（模型先问站点、再问模型是常见的两个回合）
    await answerSiteQuestionIfAsked(page);
    await redactText(page, key);
    const menu = page.locator('#choiceButtons');
    if (await menu.count() > 0) {
      console.log('  模型菜单以选项面板形式给出 ⇒ 04-dialog 裁选项区');
      await menu.screenshot({ path: path.join(outDir('tutorial-03'), '04-dialog.png') });
    } else {
      await shot(page, 'tutorial-03', '04-dialog.png');
    }

    // 05-result：真出图 + 任务面板。挡住就如实记账，不伪造。
    await sendMessage(page, '就第一个，默认那个。');
    await waitRunDone(page, 600000);
    await redactText(page, key);
    const denied = await page.evaluate(() => {
      const text = document.body.innerText || '';
      return /1014|Access Denied|访问被拒绝|余额|insufficient/i.test(text);
    });
    if (denied) {
      console.log('  ⚠ 提交被拒（权限/余额）⇒ 05-result 跳过，需要「企业级-共享 API Key」+ 充值');
      await shot(page, 'tutorial-03', '05-result.BLOCKED.png');
      return;
    }
    await openTasksPanel(page);
    await shot(page, 'tutorial-03', '05-result.png');
    await closeDialog(page, 'tasksDialog');
  },

  // 教程 4 · 出一集短剧（本地 ComfyUI 路线）
  // 口径：「本地跑，但只拍关键界面」——Agent 切换、`/` 选技能、剧本骨架、逐步确认、任务面板。
  // **不真跑 MiniMax H3 出片**（耗时长、占显存），06-result 待定。
  async 'tutorial-04'(page) {
    await selectAgent(page, '导演');
    await shot(page, 'tutorial-04', '01-agent.png');

    // 02-skill：`/` 过滤到本地路线那个技能（正文表格里的 comfyui-shortdramav2）
    await openSlashPopup(page, 'comfyui-shortdramav2');
    await shot(page, 'tutorial-04', '02-skill.png');
    await page.keyboard.press('Escape');
    await sleep(400);

    // 03-script：让 AI 照技能自带的模板搭一份骨架（只读技能 + 写一个 .md，明确不出片）
    await sendMessage(page, '先别出片。照技能里的模板给我搭一个剧本骨架，我照着改。');
    await waitRunDone(page, 300000);
    await shot(page, 'tutorial-04', '03-script.png');

    // 04-confirm：技能是「先问后做」——接着把要确认的一次性问出来
    await sendMessage(page, '把该确认的一次性问清楚，先别提交任务。');
    await waitRunDone(page, 300000);
    const choices = page.locator('#choiceButtons');
    if (await choices.count() > 0) {
      await choices.screenshot({ path: path.join(outDir('tutorial-04'), '04-confirm.png') });
      console.log('  shot 04-confirm.png（裁选项区）');
    } else {
      await shot(page, 'tutorial-04', '04-confirm.png');
    }

    await openTasksPanel(page);
    await shot(page, 'tutorial-04', '05-tasks.png');
    await closeDialog(page, 'tasksDialog');
    console.log('  待定：06-result 需要真跑一段 MiniMax H3 出片，本次未拍');
  },

  // 教程 5 · 改提示词（纯对话，无外部账号）
  // 提示词优化 Agent 出厂固定绑 h3-prompt-writing，切过去输入框会自动预填 `/h3-prompt-writing`。
  // 这是产品的真实行为（首轮冻结技能集就是靠它），照实拍。
  async 'tutorial-05'(page) {
    await selectAgent(page, '提示词优化');
    await shot(page, 'tutorial-05', '01-agent.png');

    await typeMessage(page, '一只很酷的猫，站在雨夜街头。');
    await shot(page, 'tutorial-05', '02-ask.png');

    await page.click('#sendButton');
    await waitRunDone(page);
    await shot(page, 'tutorial-05', '03-questions.png');

    await sendMessage(page, '你按常见的来就行，我要拿去出图。');
    await waitRunDone(page);
    await shot(page, 'tutorial-05', '04-output.png');
  },

  // 教程 6 · 让 AI 写个小工具
  // 真跑：工作区里造一批演示照片，让它按拍摄日期重命名。
  // 「先给我看清单，别动文件」→ 第二步再放它动手，正好把确认卡拍下来。
  async 'tutorial-06'(page) {
    prepareDemoPhotos();
    await selectAgent(page, '编程');
    await shot(page, 'tutorial-06', '01-agent.png');
    // 新建会话默认是「自动」档（`08-conversations.js:866` 硬编码），
    // 而正文的档位表写的是「确认（默认）」——要拍确认卡就必须显式切回确认档。
    await setPermissionMode(page, 'confirm');

    await typeMessage(
      page,
      '把工作区里「照片」文件夹的文件按拍摄日期重命名成 2026-09-24_001.jpg 这种格式。'
      + '先给我看一份重命名清单，别动文件。',
    );
    await shot(page, 'tutorial-06', '02-request.png');

    await page.click('#sendButton');
    await waitRunDone(page);

    await sendMessage(page, '清单没问题，按这个执行吧。');
    await page.waitForSelector('.tool-confirm', { timeout: 240000 });
    await sleep(1000);
    await shot(page, 'tutorial-06', '03-approve.png');
    await page.click('.tool-confirm .tool-confirm-approve');
    await waitRunDone(page);
    await shot(page, 'tutorial-06', '04-result.png');
  },

  // 教程 7 · 装个新技能
  async 'tutorial-07'(page) {
    await selectAgent(page, 'Skill 助手');
    await shot(page, 'tutorial-07', '01-agent.png');

    await sendMessage(page, '我想按小红书的风格写文案。');
    await waitRunDone(page);
    await shot(page, 'tutorial-07', '02-ask.png');

    await openSettings(page, 'skills');
    await shot(page, 'tutorial-07', '03-install.png');
    await closeSettings(page);

    await openSlashPopup(page);
    await shot(page, 'tutorial-07', '04-slash.png');
    await page.keyboard.press('Escape');
    await sleep(400);

    // 导演 Agent 出厂就钉了 3 个短剧 Skill —— 正文举的例子正是它
    await openAgentEditor(page, '导演', 'skills');
    await shot(page, 'tutorial-07', '05-pin-skill.png');
    await closeSettings(page);
  },

  // 教程 8 · 认识教程助手
  async 'tutorial-08'(page) {
    await selectAgent(page, '教程助手');
    // 注意：Agent 下拉是**原生 `<select>`**，弹层由系统绘制、不进页面截图。
    // 这张只能拍「已选中教程助手」的顶栏，读不出「第一位」这个信息——正文配图口径要改。
    await shot(page, 'tutorial-08', '01-dropdown.png');

    await typeMessage(page, '我想换个聊天背景。');
    await shot(page, 'tutorial-08', '02-ask.png');

    await page.click('#sendButton');
    await waitRunDone(page);
    await shot(page, 'tutorial-08', '03-answer.png');

    // 04-guessing：正文说「说得太含糊时，它不瞎猜，而是给你 2–3 个『你是不是想……』」。
    // 03-answer 已经把「聊天背景」问对了，同一句再追问就自相矛盾——**另开一条会话**，
    // 问一句真的含糊的话，让选项面板自然触发。模型措辞不保证每次都把引导语放在列表**之前**
    //（`_is_prompt_line` 要求 cue 词 / 模式标记距列表首行 ≤2 行），所以留一句兜底追问。
    //
    // 选项面板挂在 `.composer-wrap` 里、`#composerForm` **之前**（`#choiceButtons`），
    // **不在 `#messages` 里**——按 `#messages .choice-buttons` 找永远是 0 个。
    await newConversation(page, DEFAULT_MODEL);
    await selectAgent(page, '教程助手');
    await sendMessage(page, '我想让它更像我自己一点。');
    await waitRunDone(page);
    let choices = page.locator('#choiceButtons');
    if (await choices.count() === 0) {
      console.log('  引导语在列表之后（未命中 cue），追问一句逼出 cue-first 列表');
      await sendMessage(page, '说得太绕了，直接给我几个选项挑一个。');
      await waitRunDone(page);
      choices = page.locator('#choiceButtons');
    }
    if (await choices.count() === 0) throw new Error('这条回复里没有选项区，04-guessing 拍不到');
    await choices.screenshot({ path: path.join(outDir('tutorial-08'), '04-guessing.png') });
    console.log('  shot 04-guessing.png（裁选项区）');

    await openSettings(page, 'skills');
    await shot(page, 'tutorial-08', '05-skill.png');
    await closeSettings(page);
  },
};

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  let code = 0;
  let page = null;
  try {
    const fn = SHOTS[TUT];
    if (!fn) {
      console.error(`没有这篇的拍摄脚本：${TUT}\n可选：${Object.keys(SHOTS).join(', ')}`);
      code = 2;
    } else {
      console.log(`== ${TUT} @ ${BASE}`);
      page = await newPage(browser);
      await fn(page);
      console.log(`== ${TUT} done`);
    }
  } catch (err) {
    console.error('FAILED:', err.message);
    // 失败也要留证据：把当时画面存下来，否则只能靠猜
    if (page) {
      try {
        const p = path.join(outDir(TUT), '_FAILED.png');
        await page.screenshot({ path: p });
        console.error('  诊断截图:', p);
      } catch (_) { /* 页面可能已经没了 */ }
    }
    code = 1;
  } finally {
    if (pageErrors.length) {
      console.log('PAGE ERRORS:');
      pageErrors.forEach((e) => console.log('  ' + e));
    }
    await browser.close();
  }
  process.exit(code);
})();
