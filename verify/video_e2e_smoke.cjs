// 视频抽帧「前端真实反推」端到端脚本（隔离源码实例 + 真实在线多模态模型）。
//
// 验的是什么：模型在**真实前端**里收到一个上传的 mp4 后，能否自行
// probe_video → extract_frames（抽帧 + 联系表）→ vision_analyze 读图，
// 且抽出的帧图**真的落到消息媒体区**（可点开灯箱），而不是只躺在缓存目录里。
//
// 运行（必须与隔离实例同一个 shell 生命周期，见 run_video_e2e.sh）：
//   NAIBA_SMOKE_BASE=http://127.0.0.1:8797 \
//   NODE_PATH=~/node_modules \
//   node verify/video_e2e_smoke.cjs
//
// 产物：verify/video_e2e_shots/*.png + verify/video_e2e_result.json
//
// 完成判据走**后端真值**（会话详情里出现非 partial 的助手消息），不靠 DOM 类名
// ——DOM 类是易变细节，用它判定"跑完没有"会把脚本变成第二个待维护的前端。
// 前端只用来验"渲染成什么样"，用截图与 <img>/<video> 事实说话。
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8797';
const ROOT = path.resolve(__dirname, '..');
const FIXTURE = path.join(ROOT, 'tests', 'fixtures', 'sample_cut.mp4');
const SHOTS = path.join(ROOT, 'verify', 'video_e2e_shots');
const RESULT = path.join(ROOT, 'verify', 'video_e2e_result.json');
const MODEL_ID = process.env.NAIBA_E2E_MODEL || 'mimo-v2.5';
// 自然口吻、不点工具名：要验的正是「系统提示 + 附件引用行」能不能自己把模型推到抽帧。
const PROMPT = process.env.NAIBA_E2E_PROMPT
  || '看看这个视频里有什么内容。先了解一下它，再把关键画面抽出来给我看，顺便说说画面里写了什么。';
// 真实模型 + 抽帧 + 读图，单轮 1–4 分钟属正常，给足 8 分钟。
const DEADLINE_MS = Number(process.env.NAIBA_E2E_DEADLINE_MS || 8 * 60 * 1000);

const failures = [];
const notes = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}
function note(text) {
  console.log(`      · ${text}`);
  notes.push(text);
}

async function apiJson(url, options = {}) {
  const response = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  return response.json().catch(() => ({}));
}

function toolRunsOf(messages) {
  const runs = [];
  messages.forEach((message) => {
    const list = (message.metadata || {}).tool_runs;
    if (Array.isArray(list)) runs.push(...list);
  });
  return runs;
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  if (!fs.existsSync(FIXTURE)) {
    console.error(`缺少夹具视频：${FIXTURE}`);
    process.exit(2);
  }

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  const pageErrors = [];
  const confirmClicks = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));
  page.on('console', (message) => {
    if (message.type() === 'error') pageErrors.push(`console: ${message.text()}`);
  });

  // 只读工具对「宿主托管缓存」免确认，正常不该弹框；仍留一个兜底点击，
  // 万一模型去读了工作区外的路径，这里会记下「确实弹过确认」。
  const autoApprove = async () => {
    const buttons = await page.$$('button:visible');
    for (const button of buttons) {
      const text = (await button.innerText().catch(() => '')).trim();
      if (['允许', '批准', '同意', '允许一次', '全部允许'].includes(text)) {
        confirmClicks.push(text);
        await button.click().catch(() => {});
        return true;
      }
    }
    return false;
  };

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#messageInput', { timeout: 30000 });
  await page.waitForTimeout(1500);

  // 1) 选模型：目录来自供应商 /v1/models（页面加载时自动拉一次），等真模型选项出现。
  let modelReady = false;
  for (let i = 0; i < 60; i += 1) {
    const options = await page.$$eval('#composerModelSelect option', (nodes) =>
      nodes.map((node) => node.value));
    if (options.includes(MODEL_ID)) { modelReady = true; break; }
    await page.waitForTimeout(1000);
  }
  check(`模型目录里出现 ${MODEL_ID}`, modelReady, '60s 内没等到该模型选项');
  if (!modelReady) {
    await page.screenshot({ path: path.join(SHOTS, '01-模型未就绪.png'), fullPage: true });
    await browser.close();
    process.exit(1);
  }
  await page.selectOption('#composerModelSelect', MODEL_ID);
  note(`已选中会话模型 = ${MODEL_ID}`);

  // 2) 走真实上传入口：给隐藏 file input 塞文件，等价于用户点「添加文件」。
  await page.setInputFiles('#fileInput', FIXTURE);
  const chipShown = await page.waitForSelector('#pendingFiles .pending-item', { timeout: 30000 })
    .then(() => true).catch(() => false);
  check('上传后待发送区出现附件行', chipShown,
    await page.$eval('#pendingFiles', (node) => node.innerText).catch(() => '(读不到)'));
  await page.waitForTimeout(2500);
  await page.screenshot({ path: path.join(SHOTS, '01-已上传待发送.png'), fullPage: true });

  // 3) 真实发送（等价于用户敲回车 / 点发送）。
  await page.fill('#messageInput', PROMPT);
  await page.click('#sendButton');
  note(`已发送：${PROMPT}`);

  // 4) 轮询后端真值等这一轮结束：出现助手消息且不再 partial，或出现失败消息。
  const started = Date.now();
  let timedOut = false;
  let target = null;
  let messages = [];
  while (Date.now() - started < DEADLINE_MS) {
    await autoApprove();
    const list = await apiJson('/api/conversations');
    const conversations = list.conversations || list.items || [];
    target = conversations.find((item) => String(item.title || '').startsWith(PROMPT.slice(0, 12)))
      || conversations[0] || null;
    if (target) {
      const detail = await apiJson(`/api/conversations/${target.id}`);
      messages = detail.messages || [];
      const assistant = messages.filter((item) => item.role === 'assistant');
      const settled = assistant.length > 0 && assistant.every((item) => !(item.metadata || {}).partial);
      const failed = messages.some((item) => item.role === 'error' || (item.metadata || {}).error);
      if (settled || failed) break;
    }
    await page.waitForTimeout(3000);
  }
  if (Date.now() - started >= DEADLINE_MS) timedOut = true;
  const elapsed = Math.round((Date.now() - started) / 1000);
  check(`一轮在 ${DEADLINE_MS / 1000}s 内结束（实际 ${elapsed}s）`, !timedOut, '撞上超时上限');

  // 5) 后端真值：这一轮到底调了哪些工具、产物落在哪。
  const runs = toolRunsOf(messages);
  const names = runs.map((item) => String(item.tool || ''));
  const joined = JSON.stringify(messages);
  note(`后端工具调用序列：${names.join(' → ') || '(无)'}`);
  check('后端确实调用了 probe_video', names.includes('probe_video'));
  check('后端确实调用了 extract_frames', names.includes('extract_frames'));
  check('后端用 vision_analyze 真读了抽出来的帧图', names.includes('vision_analyze'));
  check('抽帧产物落在 video_frames 缓存目录', /video_frames/.test(joined));

  const extractRun = runs.find((item) => item.tool === 'extract_frames');
  let frameCount = 0;
  let sheetPath = '';
  if (extractRun && extractRun.result) {
    try {
      const parsed = typeof extractRun.result === 'string' ? JSON.parse(extractRun.result) : extractRun.result;
      frameCount = (parsed.frames || []).length;
      sheetPath = String(parsed.sheet || '');
      note(`extract_frames: mode=${parsed.mode} 帧数=${parsed.count} 联系表=${sheetPath || '(无)'}`);
      const thumbsOk = (parsed.frames || []).every((frame) => frame.thumb_path);
      check('每张帧图都带缩略图路径（前端据此出缩略图，缺了会 404）', thumbsOk);
    } catch (error) {
      note(`extract_frames 结果解析失败：${error.message}`);
    }
  }
  check('抽出了帧（>0）', frameCount > 0, `frameCount=${frameCount}`);
  check('生成了联系表', Boolean(sheetPath), 'sheet 为空');
  check('落库的助手消息里没有 error', !messages.some((item) => (item.metadata || {}).error),
    JSON.stringify(messages.filter((item) => (item.metadata || {}).error).map((i) => i.content).slice(0, 2)));

  await page.waitForTimeout(2500);
  await page.screenshot({ path: path.join(SHOTS, '02-整页结果.png'), fullPage: true });

  // 6) 前端真渲染：上传的 mp4 要是真播放器；抽出的帧图要真的在消息媒体区。
  const video = await page.evaluate(() => {
    const node = document.querySelector('#messages video');
    return node ? { src: node.getAttribute('src') || '', w: node.videoWidth, h: node.videoHeight } : null;
  });
  check('消息区把上传的 mp4 渲染成了视频元素', Boolean(video), JSON.stringify(video));

  const mediaInfo = await page.evaluate(() => {
    const imgs = [...document.querySelectorAll('#messages .message-row img')];
    return imgs.map((img) => ({
      src: img.getAttribute('src') || '',
      natural: `${img.naturalWidth}x${img.naturalHeight}`,
      broken: img.naturalWidth === 0,
    }));
  });
  const okImgs = mediaInfo.filter((item) => !item.broken);
  note(`消息区 <img> 共 ${mediaInfo.length} 张，可正常加载 ${okImgs.length} 张`);
  if (okImgs.length) {
    note(`样例：${okImgs.slice(0, 6).map((i) => `${i.src.split('/').pop()}(${i.natural})`).join(', ')}`);
  }
  check('前端渲染出了抽帧图片（帧图/联系表）', okImgs.length > 0, JSON.stringify(mediaInfo.slice(0, 3)));
  check('渲染出的图片没有破图', mediaInfo.every((item) => !item.broken),
    JSON.stringify(mediaInfo.filter((i) => i.broken).slice(0, 3)));

  // 7) 反推能力：最终答复必须是对画面内容的描述，而不是"我看不到视频"。
  const finalText = messages.filter((item) => item.role === 'assistant')
    .map((item) => String(item.content || '')).join('\n');
  note(`最终答复长度 ${finalText.length} 字`);
  check('最终答复确实描述了画面内容（非空且够长）', finalText.trim().length >= 60,
    finalText.slice(0, 80));
  check('最终答复没有出现「看不到/无法访问视频」之类推脱', !/无法(查看|访问|读取)|看不到视频/.test(finalText));

  check('页面无 JS 报错', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  if (confirmClicks.length) note(`点了 ${confirmClicks.length} 次权限确认按钮：${confirmClicks.join(',')}`);

  // 8) 媒体区特写：把最后一条含图的消息截下来（给用户看"帧图真在气泡里"）。
  const box = await page.evaluate(() => {
    const imgs = [...document.querySelectorAll('#messages .message-row img')];
    if (!imgs.length) return null;
    const row = imgs[imgs.length - 1].closest('.message-row') || imgs[imgs.length - 1].parentElement;
    row.scrollIntoView({ block: 'center' });
    const rect = row.getBoundingClientRect();
    return {
      x: Math.max(0, rect.x - 8),
      y: Math.max(0, rect.y - 8),
      width: Math.min(1440, rect.width + 16),
      height: Math.min(960, rect.height + 16),
    };
  });
  if (box) {
    await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(SHOTS, '03-媒体区特写.png'), clip: box });
  }

  fs.writeFileSync(RESULT, JSON.stringify({
    model: MODEL_ID,
    prompt: PROMPT,
    elapsed,
    tool_sequence: names,
    frame_count: frameCount,
    sheet: sheetPath,
    conversation: target ? { id: target.id, title: target.title, model_name: target.model_name } : null,
    media_info: mediaInfo,
    notes,
    failures,
  }, null, 2), 'utf-8');

  console.log(`\n== 截图目录：${SHOTS}`);
  console.log(`== 结论写入：${RESULT}`);
  console.log(failures.length ? `\nFAIL（${failures.length}）：\n- ${failures.join('\n- ')}` : '\n全部通过');

  await browser.close();
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('脚本异常：', error);
  process.exit(3);
});
