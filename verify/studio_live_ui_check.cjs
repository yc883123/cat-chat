// 工作台 UI 真连模型生成实测（发版前人工等效验证，真实 API 消耗由调用方明确授权）。
// 覆盖：普通短文生成 × N 家供应商 + 4 万字长正文条目生成一次（把「长正文在 UI 未实测」风险当场验掉）。
// 前置：源码服务已就绪（NAIBA_SRV_BASE，含真实供应商配置与访问口令 config.json）。
// 运行：node verify\studio_live_ui_check.cjs
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.NAIBA_SRV_BASE || 'http://127.0.0.1:8877';
const ROOT = path.resolve(__dirname, '..');
const CONFIG = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf-8'));
const TOKEN = String(CONFIG.access_token || '');

const PLAN = [
  { provider: 'online:63b9da15d11a', label: 'deepseek-v4.1-flash', model: 'deepseek-v4.1-flash', longBody: false },
  { provider: 'online:688f851432be', label: 'gpt-6-astra(tegp6)', model: 'gpt-6-astra', longBody: false },
  { provider: 'online:2ea3325cfa26', label: 'gpt-5.6-sol(tesol)', model: 'gpt-5.6-sol', longBody: false },
  { provider: 'online:63b9da15d11a', label: 'deepseek-v4.1-flash(长正文 vs-029)', model: 'deepseek-v4.1-flash', longBody: true },
];

const SHORT_ASK = '用一句话回答：1+1 等于几？只给答案，不要解释。';

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const consoleErrors = [];
  page.on('pageerror', (err) => consoleErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(`console.error: ${msg.text()}`);
  });

  const failures = [];
  const check = (label, ok, detail = '') => {
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? `  -> ${detail}` : ''}`);
    if (!ok) failures.push(label);
  };

  try {
    await page.goto(BASE, { waitUntil: 'load', timeout: 30000 });
    await page.waitForTimeout(2500);
    // 访问口令
    const authed = await page.evaluate(() => document.querySelector('#authDialog')?.open);
    if (authed) {
      await page.fill('#authToken' in document ? '#authToken' : '#tokenInput', TOKEN);
      await page.click('#authForm button[type="submit"]');
      await page.waitForTimeout(2500);
    }
    check('连接成功（auth 后消息区可见）', await page.isVisible('#messages'));

    // 打开工作台
    await page.click('#openStudio');
    await page.waitForTimeout(1800);
    check('工作台打开', await page.evaluate(() => document.body.dataset.studio === 'on'));

    let stepIndex = 0;
    for (const step of PLAN) {
      const tag = `[${step.label}]`;
      stepIndex += 1;
      console.log(`--- 步骤 ${stepIndex}: ${tag} 开始（provider=${step.provider}）`);
      // 选配置
      await page.selectOption('#studioProviderSelect', step.provider);
      await page.waitForTimeout(2500);
      const modelOptions = await page.evaluate(() => [...document.querySelectorAll('#studioModelSelect option')].map((o) => o.value).filter(Boolean));
      check(`${tag} 模型目录已取到`, modelOptions.length > 0, `count=${modelOptions.length}`);
      // 显式选模型：目录里有就 selectOption；没有（如部分中转不提供 /models）
      // 就注入该模型名后选中——保证每一步都用"目录/配置里的真实模型名"出请求。
      await page.fill('#studioSearch', '');
      const ensureModel = await page.evaluate((model) => {
        const select = document.querySelector('#studioModelSelect');
        select.value = '';
        if (model) {
          if (![...select.options].some((o) => o.value === model)) {
            const opt = document.createElement('option');
            opt.value = model;
            opt.textContent = model;
            select.append(opt);
          }
          select.value = model;
          select.dispatchEvent(new Event('change'));
        }
        return select.value;
      }, step.model || '');
      check(`${tag} 模型已选定`, Boolean(ensureModel), ensureModel);
      // 条目选择
      if (step.longBody) {
        await page.fill('#studioSearch', '古风美女');
        await page.waitForTimeout(1200);
        await page.locator('.studio-row').first().click();
        await page.waitForTimeout(800);
      } else {
        await page.fill('#studioSearch', '人像反推 9 分镜');
        await page.waitForTimeout(1000);
        await page.locator('.studio-row').first().click();
        await page.waitForTimeout(800);
      }
      // 我的输入
      try {
        await page.fill('#studioUserRequest', step.longBody
          ? '只挑出其中"跪地抚琴的女子"那一条的完整正图提示词，直接输出结果，不要任何解释。'
          : SHORT_ASK);
        await page.click('#studioGenerate');
      } catch (subErr) {
        failures.push(`exception(sub): ${subErr.message} @step ${stepIndex} ${step.label}`);
        console.log(`   子步骤异常：${subErr.message}`);
        continue;
      }
      const startedAt = Date.now();
      // 等待终态（hint 文案变化 / 继续追问按钮出现 / 取消或重试按钮出现）
      let terminalState = null;
      let toastText = '';
      for (let i = 0; i < 160; i += 1) {
        await page.waitForTimeout(1500);
        terminalState = await page.evaluate(() => ({
          hint: document.querySelector('#studioTaskHint')?.textContent || '',
          result: document.querySelector('#studioResultStream')?.textContent || '',
          handoffVisible: !document.querySelector('#studioHandoff')?.hidden,
          retryVisible: !document.querySelector('#studioRetryTask')?.hidden,
          toast: (document.querySelector('#toast')?.textContent || '').trim(),
        }));
        if (terminalState.hint.includes('完成') || terminalState.hint.includes('失败') || terminalState.hint.includes('取消') || terminalState.handoffVisible || terminalState.retryVisible) break;
        const toastNow = terminalState.toast || '';
        if (toastNow && (toastNow.includes('请选择模型') || toastNow.includes('失败'))) {
          terminalState.hint = toastNow;
          break;
        }
      }
      toastText = terminalState.toast || '';
      const elapsedS = Math.round((Date.now() - startedAt) / 1000);
      const okDone = terminalState.handoffVisible && (terminalState.result || '').trim().length > 0;
      check(`${tag} 生成完成（${elapsedS}s）`, okDone, `hint=${terminalState.hint}`);
      console.log(`   结果：${String(terminalState.result).trim().replace(/\s+/g, ' ').slice(0, 160)}`);
      console.log(`--- 步骤 ${stepIndex}: ${tag} 结束`);
      if (!okDone) continue;
      // 本轮做下一个模型前 reset 选中（历史列表点击会刷新，保持 UI 走真实路径）
      await page.evaluate(() => {
        const meta = document.querySelector('#studioResultMeta');
        if (meta) meta.scrollIntoView();
      });
    }

    const errorsToReport = consoleErrors.filter((line) => !line.includes('status of 4'));
    check('无前端错误（页面错误/控制台错误，仅容 4xx 资源型噪声）', errorsToReport.length === 0, JSON.stringify(errorsToReport.slice(0, 4)));
  } catch (e) {
    failures.push(`exception: ${e.message}`);
    console.log('外层异常堆栈:');
    console.log(e.stack);
  } finally {
    await browser.close();
  }
  if (failures.length) {
    console.log('失败项:', JSON.stringify(failures, null, 1));
    process.exit(1);
  }
  console.log('live UI check 全部通过');
})();
