// 「立即更新」按钮状态冒烟（2026-10-23 事故回归）。
//
// 事故：下载中途断网失败后，再点「检查更新」显示有新版本，但「立即更新」按下去没反应；
// 下载完选择「不要更新」后同样点不动。根因是 renderUpdateStatus 只翻 hidden 不翻 disabled，
// 而点按钮时置的 disabled 只有请求本身抛错才会复位。
//
// 为什么必须连真后端：设置页、版本下拉、按钮绑定都来自真实页面；但源码实例的 mode 是
// 'source'（本来就不给一键更新），所以这里用 Playwright 路由把 /api/bootstrap 的 update
// 段改写成「打包版 + 发现新版本」，再伪造 /api/update 各态推进前端——不真发一次版。
//
// 用法：
//   1) NAIBA_TMP_PORT=8799 NAIBA_TMP_ROOT=verify/_tmp_upd_probe2 .venv\Scripts\python.exe verify\_serve_tmp.py
//   2) set NODE_PATH=<项目根>\node_modules && node verify\update_install_button_smoke.cjs
const { chromium } = require('playwright');
const path = require('path');

const PORT = Number(process.env.NAIBA_UPD_SMOKE_PORT || 8799);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOT_DIR = path.resolve(__dirname, '..', 'verify');

const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const RELEASES = [
  { tag: 'v9.9.9-beta', version: '9.9.9-beta', published_at: '', release_url: '', release_notes: ['修复'], installable: true, current: false },
  { tag: 'v3.6.3-beta', version: '3.6.3-beta', published_at: '', release_url: '', release_notes: [], installable: true, current: true },
];

const ARTIFACT_DIR = 'C:\\Users\\somebody\\AppData\\Local\\NaibaChat\\update';
const artifactsAbsent = { directory: ARTIFACT_DIR, file_name: '', size_bytes: 0, state: 'absent' };
const artifactsDownloading = { directory: ARTIFACT_DIR, file_name: 'naiba-chat-aaaaaaaaaaaa.download', size_bytes: 31 * 1024 * 1024, state: 'downloading' };
const artifactsReady = { directory: ARTIFACT_DIR, file_name: 'naiba-chat-ffffffffffff.download', size_bytes: 96 * 1024 * 1024, state: 'ready' };
const artifactsStuck = { directory: ARTIFACT_DIR, file_name: 'naiba-chat-bbbbbbbbbbbb.download', size_bytes: 96 * 1024 * 1024, state: 'cleanup_failed' };

(async () => {
  const browser = await chromium.launch({ channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const consoleErrors = [];
  page.on('pageerror', (error) => consoleErrors.push(String(error)));
  page.on('dialog', (dialog) => dialog.accept());

  const baseStatus = {
    repository: 'yc883123/naiba-chat',
    supported: true,
    mode: 'executable',
    current_version: '3.6.3-beta',
    current_commit: '5'.repeat(40),
    phase: 'available',
    error: '',
    checked_at: 1,
    update_available: true,
    latest_version: '9.9.9-beta',
    latest_commit: 'f'.repeat(40),
    release_notes: ['修复'],
    published_at: '',
    release_url: '',
    manual_update_available: false,
    manual_release_url: '',
    releases: RELEASES,
    pending_verification: { pending: false },
    source_dirty: false,
    download: { received: 0, total: 0, speed: 0, percent: 0, stalled: false },
    can_cancel: false,
    ready: {},
    can_apply: false,
    proxy: { mode: 'inherit', url: '', note: '' },
    artifacts: artifactsAbsent,
  };
  const downloading = {
    ...baseStatus,
    phase: 'downloading',
    can_cancel: true,
    download: { received: 30 * 1024 * 1024, total: 96 * 1024 * 1024, speed: 1024 * 1024, percent: 31, stalled: false },
    artifacts: artifactsDownloading,
  };
  const failed = { ...baseStatus, phase: 'error', error: '安装更新失败：网络中断（更新链路：直连）', checked_at: 2, artifacts: artifactsAbsent };
  const ready = {
    ...baseStatus,
    phase: 'ready',
    can_cancel: false,
    can_apply: true,
    ready: { version: '9.9.9-beta', commit: 'f'.repeat(40), downloaded_at: 1 },
    artifacts: artifactsReady,
  };

  let status = { ...baseStatus };

  await page.route('**/api/bootstrap', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.update = { ...status };
    await route.fulfill({ response, body: JSON.stringify(payload), headers: { ...response.headers(), 'content-type': 'application/json' } });
  });
  await page.route('**/api/update', (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(status) }));
  await page.route('**/api/update/install', (route) => {
    status = { ...downloading };
    route.fulfill({ contentType: 'application/json', body: JSON.stringify(status) });
  });
  await page.route('**/api/update/check', (route) => {
    status = { ...baseStatus, phase: 'checking', checked_at: 10 };
    setTimeout(() => { status = { ...baseStatus, checked_at: 11 }; }, 400);
    route.fulfill({ contentType: 'application/json', body: JSON.stringify(status) });
  });
  await page.route('**/api/update/cancel', (route) => {
    status = { ...baseStatus, checked_at: 12 };
    route.fulfill({ contentType: 'application/json', body: JSON.stringify(status) });
  });
  await page.route('**/api/update/discard', (route) => {
    status = { ...baseStatus, checked_at: 13 };
    route.fulfill({ contentType: 'application/json', body: JSON.stringify(status) });
  });

  await page.goto(`${BASE}/index.html`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#messageInput', { timeout: 15000 });
  await page.evaluate(() => document.getElementById('onboardingDialog')?.remove());
  await sleep(800);

  const visible = (id) => page.evaluate((elementId) => {
    const el = document.getElementById(elementId);
    return Boolean(el) && getComputedStyle(el).display !== 'none';
  }, id);
  const enabled = (id) => page.evaluate((elementId) => !document.getElementById(elementId).disabled, id);
  const openUpdatesTab = async () => {
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('[data-settings-tab="updates"]');
    await sleep(600);
  };

  await openUpdatesTab();
  check('① 发现新版本：立即更新可见且可点', (await visible('installUpdate')) && (await enabled('installUpdate')));

  const artifactsGeom = await page.evaluate(() => {
    const code = document.getElementById('updateArtifactsDir');
    const panel = document.getElementById('updateArtifacts');
    const codeBox = code.getBoundingClientRect();
    const panelBox = panel.getBoundingClientRect();
    return {
      text: code.textContent,
      userSelect: getComputedStyle(code).userSelect,
      overflow: Math.round(codeBox.right - panelBox.right),
      file: document.getElementById('updateArtifactsFile').textContent,
    };
  });
  check('① 更新产物：显示后端给的更新文件夹路径', artifactsGeom.text === ARTIFACT_DIR, JSON.stringify(artifactsGeom));
  check('① 更新产物：长路径可选中且不溢出面板', artifactsGeom.userSelect === 'text' && artifactsGeom.overflow <= 1,
    JSON.stringify(artifactsGeom));
  check('① 更新产物：没有下载包时如实说明', artifactsGeom.file.includes('当前没有保留的更新包'), artifactsGeom.file);
  await page.screenshot({ path: path.join(SHOT_DIR, 'update_artifacts_row.png') });

  // 窄窗口：长路径换行、不溢出，也不压住下面那排按钮
  await page.setViewportSize({ width: 390, height: 844 });
  await sleep(400);
  const narrow = await page.evaluate(() => {
    const panel = document.getElementById('updateArtifacts');
    const code = document.getElementById('updateArtifactsDir');
    const actions = document.getElementById('checkUpdate').closest('.update-actions');
    const panelBox = panel.getBoundingClientRect();
    const actionsBox = actions.getBoundingClientRect();
    return {
      panelRight: Math.round(panelBox.right),
      viewport: window.innerWidth,
      overlapsActions: panelBox.bottom > actionsBox.top + 1,
      codeWraps: code.scrollWidth <= code.clientWidth + 1,
    };
  });
  check('① 窄窗口：产物行不溢出、长路径换行、不压住按钮',
    narrow.panelRight <= narrow.viewport + 1 && !narrow.overlapsActions && narrow.codeWraps,
    JSON.stringify(narrow));
  await page.screenshot({ path: path.join(SHOT_DIR, 'update_artifacts_row_narrow.png') });
  await page.setViewportSize({ width: 1280, height: 900 });
  await sleep(300);

  await page.click('#installUpdate');
  await sleep(400);
  check('② 点击后下载中：按钮隐藏', !(await visible('installUpdate')));
  check('② 下载中：按钮置灰（防重复提交）', !(await enabled('installUpdate')));
  const downloadingArtifact = await page.textContent('#updateArtifactsFile');
  check('② 下载中：显示正在写的文件名与已写入大小',
    downloadingArtifact.includes('正在下载') && downloadingArtifact.includes('naiba-chat-aaaaaaaaaaaa.download')
      && downloadingArtifact.includes('已写入'), downloadingArtifact);

  status = { ...downloading, artifacts: { ...artifactsStuck } };
  await sleep(900);
  const stuckArtifact = await page.textContent('#updateArtifactsFile');
  check('② 清理失败：如实显示残留文件与占用',
    stuckArtifact.includes('未能清理') && stuckArtifact.includes('96.0 MB')
      && !stuckArtifact.includes('已清理'), stuckArtifact);
  check('② 清理失败：给出警示色', await page.evaluate(() => document.getElementById('updateArtifactsFile').classList.contains('update-artifacts-warn')));

  // 断网 → 后端 phase=error，前端 500ms 轮询渲染该状态（事故现场）
  status = { ...failed };
  await sleep(1200);
  check('③ 断网失败后：按钮重新可见', await visible('installUpdate'));
  check('③ 断网失败后：按钮恢复可点（核心回归）', await enabled('installUpdate'),
    '失败后仍置灰 = 用户必须重启应用才能重试');
  check('③ 失败原因展示在面板上', (await page.textContent('#updateMessage')).includes('安装更新失败'));
  check('③ 失败并与磁盘一致：报「没有保留」',
    (await page.textContent('#updateArtifactsFile')).includes('当前没有保留的更新包'));

  // 再点一次仍能发起下载（说明按钮不只是"看起来能点"）
  await page.click('#installUpdate');
  await sleep(400);
  check('③ 失败后可再次发起下载', !(await visible('installUpdate')));

  // 取消下载 → 回到可安装（取消接口自己把状态改回 available）
  await sleep(600);
  await page.click('#cancelUpdate');
  await sleep(700);
  check('④ 取消下载后：按钮恢复可点', (await visible('installUpdate')) && (await enabled('installUpdate')));

  // 下载完成 → 「不要更新」：再起一次下载，让快速轮询在跑，然后推到 ready
  await page.click('#installUpdate');
  await sleep(500);
  status = { ...ready };
  await sleep(1400);
  check('⑤ 下载完成：出现立即重启、隐藏立即更新', (await visible('restartNow')) && !(await visible('installUpdate')));
  const readyArtifact = await page.textContent('#updateArtifactsFile');
  check('⑤ 下载完成：显示安装包名、体积与「重启后生效」',
    readyArtifact.includes('naiba-chat-ffffffffffff.download') && readyArtifact.includes('96.0 MB')
      && readyArtifact.includes('重启后生效'), readyArtifact);
  await page.click('#restartLater');
  await sleep(300);
  check('⑤ 稍后重启：转为常驻待重启横幅', await visible('updateReady'));
  await page.click('#readyDiscard');
  await sleep(700);
  check('⑥ 放弃更新后：按钮重新可见', await visible('installUpdate'));
  check('⑥ 放弃更新后：按钮恢复可点（第二个投诉）', await enabled('installUpdate'));
  check('⑥ 放弃更新后：产物信息回到「没有保留」',
    (await page.textContent('#updateArtifactsFile')).includes('当前没有保留的更新包'));

  // ⑧ 重开页面时后端仍在下载：必须自动接上轮询（否则进度条永远停在旧值）
  status = { ...downloading };
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#messageInput', { timeout: 15000 });
  await page.evaluate(() => document.getElementById('onboardingDialog')?.remove());
  await sleep(1000);
  check('⑧ 重开页面时正在下载：悬浮卡接上并显示进度', await visible('updateFloat'));
  status = { ...ready };
  await sleep(1600);
  check('⑧ 无需任何点击：自动切到「更新待重启」', (await page.textContent('#updateFloatTitle')).includes('待重启'),
    await page.textContent('#updateFloatTitle'));

  check('⑦ 页面无 JS 异常', consoleErrors.length === 0, consoleErrors.join(' | '));

  await browser.close();
  console.log(failures.length ? `\nFAILED (${failures.length}): ${failures.join(', ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})().catch((error) => {
  console.error('SMOKE CRASH:', error);
  process.exit(2);
});
