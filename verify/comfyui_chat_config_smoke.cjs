// 造梦机「对话参数」弹窗冒烟（由 verify/comfyui_chat_config_smoke.py 起隔离 server 后调用）。
// 验证：工作流卡入口、绑定添加/保存/重开回显/移除/重置、指纹提示、桌面与 375px 视口。
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8833';
const WORKFLOW = process.env.NAIBA_SMOKE_WORKFLOW || 'cw-chatcfg01';
const SHOT_DESKTOP = path.join(__dirname, '_comfyui_chat_config_desktop.png');
const SHOT_MOBILE = path.join(__dirname, '_comfyui_chat_config_375.png');

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

// 前端运行时检查：pageerror 一出现就当场记 FAIL（带消息）——§九.167 的教训。
const pageErrors = [];
function checkNoScriptError(label) {
  const scriptErrors = pageErrors.filter((line) => !line.startsWith('console.error: Failed to load resource'));
  check(label, scriptErrors.length === 0, scriptErrors.join(' | ').slice(0, 600));
  pageErrors.length = 0;
}

async function openComfyuiWorkflows(page) {
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1000);
  if (await page.locator('#onboardingDialog').isVisible().catch(() => false)) {
    await page.click('#onboardingSkip');
    await page.waitForTimeout(400);
  }
  await page.click('#openStudio');
  await page.waitForTimeout(500);
  await page.click('[data-studio-page="comfyui"]');
  await page.waitForTimeout(800);
  // 工作流分页卡在「工作流」档：默认档是「单次生成」，先切过去。
  await page.click('[data-comfyui-pane="workflows"]');
  await page.waitForTimeout(600);
  await page.waitForSelector(`[data-comfyui-chat-config="${WORKFLOW}"]`, { timeout: 8000 });
}

async function openChatConfigDialog(page) {
  await page.click(`[data-comfyui-chat-config="${WORKFLOW}"]`);
  await page.waitForSelector('#comfyuiChatConfigDialog[open]', { timeout: 5000 });
  await page.waitForTimeout(300);
}

async function runDesktopChecks(page) {
  try {
    await openComfyuiWorkflows(page);
    check('工作流卡有「对话参数」入口',
      await page.locator(`[data-comfyui-chat-config="${WORKFLOW}"]`).count() === 1);
    checkNoScriptError('工作流页渲染无脚本错误');

    await openChatConfigDialog(page);
    check('弹窗打开且标题带工作流名',
      /对话参数 · 对话参数冒烟流/.test(await page.textContent('#comfyuiChatConfigTitle') || ''));
    // §九.169 同款坑的守门：全局 `label input` 是 40px 高的文本框造型，会撑大行内
    // checkbox（用户实测抓过 select 被压成单行的同类塌陷）——量到勾选框保持原生小尺寸。
    const slotBox = await page.evaluate(() => {
      const box = document.querySelector('#comfyuiChatConfigSlots input[type="checkbox"]');
      if (!box) return { found: false };
      const rect = box.getBoundingClientRect();
      return { found: true, w: Math.round(rect.width), h: Math.round(rect.height) };
    });
    check('槽位勾选框保持原生小尺寸（未被全局样式撑成大方块）',
      slotBox.found && slotBox.w <= 24 && slotBox.h <= 24, JSON.stringify(slotBox));
    check('指纹提示存在',
      /绑定指纹/.test(await page.textContent('#comfyuiChatConfigFingerprint') || ''));
    check('空配置提示（至少建议指认正向提示词）',
      /还没有指认任何参数/.test(await page.textContent('#comfyuiChatConfigList') || ''));
    checkNoScriptError('打开弹窗无脚本错误');

    // 添加一条正向提示词绑定：用途默认就是 positive_prompt，显示名自动填。
    await page.waitForFunction(() => document.querySelectorAll('#comfyuiChatConfigSlots input[type="checkbox"]').length > 0,
      { timeout: 5000 });
    const optionTexts = await page.$$eval('#comfyuiChatConfigSlots .comfyui-chat-config-slot-text', (rows) => rows.map((row) => row.textContent));
    check('正向提示词用途列出文本槽（含映射标签）',
      optionTexts.some((text) => /text/.test(text) && !/宽度|height/i.test(text)),
      optionTexts.join(' | ').slice(0, 200));
    await page.check('#comfyuiChatConfigSlots input[type="checkbox"][value="5|text"]');
    await page.fill('#comfyuiChatConfigName', '主体提示词');
    await page.click('#comfyuiChatConfigAddBtn');
    await page.waitForTimeout(200);
    check('绑定行出现（用途徽标 + 显示名）',
      /主体提示词/.test(await page.textContent('#comfyuiChatConfigList') || '')
      && /正向提示词/.test(await page.textContent('#comfyuiChatConfigList') || ''));
    checkNoScriptError('添加绑定无脚本错误');

    // 空显示名 / 未选槽位要在对话框内报错，不静默。
    await page.click('#comfyuiChatConfigAddBtn');
    await page.waitForTimeout(150);
    check('缺显示名在弹窗内报错',
      /显示名/.test(await page.textContent('#comfyuiChatConfigError') || ''),
      await page.textContent('#comfyuiChatConfigError') || '');

    await page.click('#comfyuiChatConfigSave');
    await page.waitForFunction(() => !document.querySelector('#comfyuiChatConfigDialog').open,
      { timeout: 8000 });
    check('保存后弹窗关闭', true);
    checkNoScriptError('保存无脚本错误');

    // 重开回显：服务端已保存的绑定要出现在清单里。
    await openChatConfigDialog(page);
    check('重开回显已保存绑定',
      /主体提示词/.test(await page.textContent('#comfyuiChatConfigList') || ''));

    // 移除 + 保存 + 重置（confirm 接受）。
    await page.click('[data-chat-config-remove]');
    await page.waitForTimeout(200);
    check('移除后清单回到空态',
      /还没有指认任何参数/.test(await page.textContent('#comfyuiChatConfigList') || ''));
    page.once('dialog', (dialog) => dialog.accept());
    await page.click('#comfyuiChatConfigReset');
    await page.waitForTimeout(500);
    check('重置成功有提示',
      /已重置对话参数/.test(await page.locator('#toast').textContent() || ''),
      await page.locator('#toast').textContent() || '');
    checkNoScriptError('移除/重置无脚本错误');
    await page.click('#comfyuiChatConfigCancel');
    await page.waitForTimeout(300);

    await page.screenshot({ path: SHOT_DESKTOP, fullPage: false });
  } catch (error) {
    check('桌面流程可走通', false, String(error).slice(0, 400));
  }
}

async function runMobileChecks(page) {
  // 与既有造梦机冒烟同款做法：同一页缩放视口（手机顶栏没有桌面入口按钮，
  // 重新 goto + 点 #openStudio 会点不到）。
  await page.setViewportSize({ width: 375, height: 812 });
  await page.waitForTimeout(600);
  try {
    // 工作流档在手机上被切走过：重新选一次工作流档，再开弹窗。
    if (!(await page.locator(`[data-comfyui-chat-config="${WORKFLOW}"]`).isVisible().catch(() => false))) {
      await page.click('[data-comfyui-pane="workflows"]');
      await page.waitForTimeout(500);
    }
    await openChatConfigDialog(page);
    await page.waitForFunction(() => document.querySelectorAll('#comfyuiChatConfigSlots input[type="checkbox"]').length > 0,
      { timeout: 5000 });
    // 375px：弹窗不溢出视口（横向滚动条不得出现），按钮全部可见可点。
    const overflow = await page.evaluate(() => {
      const dialog = document.querySelector('#comfyuiChatConfigDialog');
      const rect = dialog.getBoundingClientRect();
      return {
        scrollOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
        dialogOverflow: rect.left < -1 || rect.right > window.innerWidth + 1,
      };
    });
    check('375px 弹窗不溢出视口', !overflow.scrollOverflow && !overflow.dialogOverflow, JSON.stringify(overflow));
    await page.fill('#comfyuiChatConfigName', '手机提示词');
    await page.check('#comfyuiChatConfigSlots input[type="checkbox"][value="5|text"]');
    await page.click('#comfyuiChatConfigAddBtn');
    await page.waitForTimeout(200);
    check('375px 也能添加绑定',
      /手机提示词/.test(await page.textContent('#comfyuiChatConfigList') || ''));
    checkNoScriptError('375px 全程无脚本错误');
    await page.screenshot({ path: SHOT_MOBILE, fullPage: false });
    await page.click('#comfyuiChatConfigCancel');
  } catch (error) {
    check('375px 流程可走通', false, String(error).slice(0, 400));
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`);
  });
  try {
    await runDesktopChecks(page);
    await runMobileChecks(page);
  } finally {
    await browser.close();
  }
  if (failures.length) {
    console.log(`\n${failures.length} 项失败：\n- ${failures.join('\n- ')}`);
    process.exit(1);
  }
  console.log('\n全部通过');
})();
