// 把 docs/tutorial-XX/_tutorial.html 打印成 A4 PDF（走 CDP，流式回传）。
// _tutorial.html 由 scripts/make_tutorial_html.py 从 README.md 生成（打完可删）。
// 技术口径与 scripts/make_pdf.mjs 一致：transferMode: 'ReturnAsStream' + IO.read 分块取，
// 绕开大 base64 单消息回传导致的无限挂起。
//
// 用法：
//   node scripts/make_tutorial_pdf.mjs                          # docs 下所有教程（跳过没生成 HTML 的）
//   node scripts/make_tutorial_pdf.mjs docs/tutorial-02-comfyui  # 只打这一篇
//
// 输出文件名从 README 的 H1 派生（去掉「N 分钟xx：」这段前缀），不再写死某一篇的目录名——
// 旧版本把目录名硬编码成 tutorial-comfyui-3min，教程改名后这个脚本就废了。
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ARGS = process.argv.slice(2).filter((a) => !a.startsWith('-'));

const CHROME = process.env.NAIBA_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = Number(process.env.NAIBA_CDP_PORT || 9224);
const PROFILE = path.join(os.tmpdir(), `naiba-tut-pdf-${Date.now()}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const DOCS = path.join(ROOT, 'docs');

function targetDirs() {
  if (ARGS.length) return ARGS.map((a) => path.resolve(ROOT, a));
  return fs
    .readdirSync(DOCS, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && entry.name.startsWith('tutorial-'))
    .map((entry) => path.join(DOCS, entry.name))
    .sort();
}

// H1 形如「3 分钟上手：用 Cat Chat 调用 ComfyUI 生成图片（小白版）」：取冒号后半段当文件名，
// 洗掉 Windows 不允许的字符。再前置目录名里的序号（`tutorial-01-...` → `01-`），
// 否则 8 份 PDF 丢进同一个文件夹后既排不出顺序、也看不出是哪一篇。
// 拿不到标题就退回目录名，绝不产出空名。
function pdfNameFor(dir) {
  const readme = path.join(dir, 'README.md');
  let title = '';
  if (fs.existsSync(readme)) {
    const hit = fs.readFileSync(readme, 'utf-8').match(/^#\s+(.+)$/m);
    title = hit ? hit[1].trim() : '';
  }
  const short =
    title.replace(/^[^：:]*[：:]\s*/, '').trim() || title || path.basename(dir);
  const order = (path.basename(dir).match(/^tutorial-(\d+)/) || [])[1];
  const body = short.replace(/[\\/:*?"<>|]/g, '').trim();
  return `${order ? `${order}-` : ''}${body}.pdf`;
}

const jobs = targetDirs()
  .map((dir) => ({
    dir,
    html: path.join(dir, '_tutorial.html'),
    out: path.join(dir, pdfNameFor(dir)),
  }))
  .filter((job) => {
    if (!fs.existsSync(job.html)) {
      console.log('跳过（没有 _tutorial.html，先跑 make_tutorial_html.py）：', job.dir);
      return false;
    }
    return true;
  });

if (!jobs.length) throw new Error('没有可打印的目标：docs 下找不到 tutorial-*/_tutorial.html');

const chrome = spawn(CHROME, [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${PROFILE.replace(/\\/g, '/')}`,
  '--no-first-run', '--no-default-browser-check',
], { stdio: 'ignore' });

let ws;
let ok = false;
try {
  let ver = null;
  for (let i = 0; i < 40 && !ver; i += 1) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) ver = await r.json();
    } catch (_) { /* 还没起来 */ }
    if (!ver) await sleep(500);
  }
  if (!ver) throw new Error('CDP 未就绪');

  ws = new WebSocket(ver.webSocketDebuggerUrl);
  let seq = 0;
  const pending = new Map();
  ws.addEventListener('message', (e) => {
    const msg = JSON.parse(e.data);
    if (msg.id && pending.has(msg.id)) { const p = pending.get(msg.id); pending.delete(msg.id); p(msg); }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });

  const send = (method, params = {}, sessionId, timeoutMs = 20000) => {
    seq += 1;
    const msg = { id: seq, method, params };
    if (sessionId) msg.sessionId = sessionId;
    return Promise.race([
      new Promise((res) => { pending.set(seq, res); ws.send(JSON.stringify(msg)); }),
      new Promise((_, rej) => setTimeout(() => rej(new Error(`TIMEOUT ${method}`)), timeoutMs)),
    ]);
  };

  for (const job of jobs) {
    const URL = `file:///${job.html.replace(/\\/g, '/')}`;
    const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' }, undefined, 30000);
    const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true }, undefined, 30000);
    await send('Page.enable', {}, sessionId, 30000);
    await send('Page.navigate', { url: URL }, sessionId, 60000);
    await sleep(4000); // 等图片解码完，否则 PDF 里会缺图

    const printed = await send('Page.printToPDF', {
      printBackground: true,
      paperWidth: 8.27, paperHeight: 11.69,          // A4
      marginTop: 0.4, marginBottom: 0.4, marginLeft: 0.4, marginRight: 0.4,
      scale: 0.85,
      transferMode: 'ReturnAsStream',
    }, sessionId, 180000);

    const stream = printed?.result?.stream;
    if (!stream) throw new Error('printToPDF 未返回流：' + JSON.stringify(printed).slice(0, 300));

    const chunks = [];
    for (let i = 0; i < 10000; i += 1) {
      const r = await send('IO.read', { handle: stream, size: 1 << 20 }, sessionId, 60000);
      const d = r?.result;
      if (!d) throw new Error('IO.read 无结果：' + JSON.stringify(r).slice(0, 200));
      if (d.data) chunks.push(Buffer.from(d.data, d.base64Encoded ? 'base64' : 'utf8'));
      if (d.eof) break;
    }
    await send('IO.close', { handle: stream }, sessionId, 20000).catch(() => {});

    const buf = Buffer.concat(chunks);
    if (buf.length < 10000) throw new Error('PDF 过小，疑似失败：' + buf.length + ' 字节');
    fs.writeFileSync(job.out, buf);
    console.log('wrote', path.relative(ROOT, job.out), (buf.length / 1024 / 1024).toFixed(2), 'MB');
    await send('Target.closeTarget', { targetId }, undefined, 20000).catch(() => {});
  }
  ok = true;
} finally {
  try { ws?.close(); } catch (_) {}
  try { chrome.kill(); } catch (_) {}
  await sleep(600);
  try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (_) {}
}

if (!ok) throw new Error('PDF 未全部生成');
process.exit(0);
