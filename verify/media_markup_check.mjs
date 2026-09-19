// P3 渲染校验（无浏览器）：把 03-media.js 的纯渲染函数装进桩环境执行，断言产出 HTML。
// 浏览器冒烟在本沙箱被拦（Edge spawn），这里用"真实执行 + 字符串断言"补上最小防线。
import { readFileSync } from 'node:fs';

const url = new URL('../public/js/03-media.js', import.meta.url);
const source = readFileSync(url, 'utf8')
  .replace(/^import .*$/gm, '')
  .replace(/^export /gm, '');

const state = {
  token: 'TOKEN',
  bootstrap: {
    media_exts: {
      exts: {
        image: ['.png', '.jpg', '.jpeg', '.webp', '.gif'],
        video: ['.mp4', '.webm', '.mov', '.m4v', '.ogv'],
        audio: ['.wav', '.mp3', '.m4a', '.ogg', '.flac'],
      },
    },
  },
  pendingFiles: [],
};
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[ch]));
const stub = {
  $: () => null,
  api: async () => ({}),
  draggedFileCache: new Map(),
  escapeHtml,
  state,
  // 与 01-core.localFileUrl 逐字同口径（本地路径的 URL 形状唯一定义在那里）。
  // 缺了它 fileUrl 会抛 ReferenceError，整支检查在第一条断言前就崩——
  // 这类桩缺口属于"静默失效的防线"，出现即补，不能留。
  localFileUrl: (source) => `/api/file?token=${encodeURIComponent(state.token)}&path=${encodeURIComponent(String(source || ''))}`,
  toast: () => {},
  markdown: (text) => String(text || ''),
  selectedProvider: () => null,
  renderPendingFiles: () => {},
  document: { addEventListener() {}, querySelector: () => null, createElement: () => ({ style: {}, classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, append() {} }) },
  window: { addEventListener() {}, setTimeout, innerWidth: 1280, innerHeight: 800 },
  location: { href: 'http://127.0.0.1:8765/' },
  navigator: {},
  fetch: async () => { throw new Error('fetch stub'); },
  btoa: () => '',
  atob: () => '',
  setTimeout,
  clearTimeout,
  console,
};

const factory = new Function(
  ...Object.keys(stub),
  `${source}\n;return { toolRunMarkup, toolMediaMarkup, mediaTruncatedNotice, mediaMarkup, mediaKind, fileUrl, inlineMediaSources, remainingAttachments, uploadedFileMarkup, activityMarkup };`,
);
const media = factory(...Object.values(stub));

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${!ok && detail ? `  -> ${detail}` : ''}`);
  if (!ok) failures.push(label);
}

// 1. 工具块 + 媒体块：媒体在 </details> 之后（折叠工具块不会把媒体一起藏起来）
const run = {
  tool: 'vision_image_ops',
  success: true,
  arguments: { op: 'crop' },
  result: '{"path":"C:\\\\a.png"}',
  media: [{ kind: 'image', name: 'a.png', source: 'C:\\a.png', thumb_path: 'C:\\a_thumb.webp' }],
};
const html = media.toolRunMarkup(run);
check('toolRunMarkup 含 tool-media 块', html.includes('<div class="tool-media">'), html.slice(0, 200));
check('媒体块紧跟工具块之后', /<\/details><div class="tool-media">/.test(html), html.slice(0, 240));
check('图片走 /api/file 缩略图 URL', html.includes('/api/file?token=TOKEN') && html.includes('a_thumb.webp'), html.slice(0, 400));
check('缩略图带 data-large-url（可开灯箱）', html.includes('data-large-url='), html.slice(0, 400));

// 2. 无媒体 → 不产出空块
check('无媒体时 toolMediaMarkup 为空', media.toolMediaMarkup({}) === '', media.toolMediaMarkup({}));
check('无媒体时 toolRunMarkup 无 tool-media', !media.toolRunMarkup({ tool: 'read_file', success: true }).includes('tool-media'));

// 3. 截断提示（不静默）
const truncated = media.toolMediaMarkup({ media: [], media_truncated: { total: 25, shown: 20, kinds: {} } });
check('截断提示文案', truncated.includes('共 25 个媒体，仅显示前 20 个'), truncated);
check('未截断时无提示', media.mediaTruncatedNotice({ total: 3, shown: 3 }) === '', '');
check('提示可单独渲染', media.mediaTruncatedNotice({ total: 30, shown: 20 }).includes('media-truncated'));

// 4. 视频/音频就地渲染
const video = media.mediaMarkup([{ kind: 'video', name: 'v.mp4', source: 'C:\\v.mp4' }]);
const audio = media.mediaMarkup([{ kind: 'audio', name: 'a.mp3', source: 'C:\\a.mp3' }]);
check('视频 <video>', video.includes('<video'), video);
check('音频 <audio>', audio.includes('<audio'), audio);

// 5. 就地来源集合（末尾网格去重依据）
const inline = media.inlineMediaSources({
  tool_runs: [{ media: [{ source: 'C:\\a.png' }] }],
  activity: [{ type: 'tool', run: { media: [{ source: 'C:\\b.mp4' }] } }, { type: 'prose', text: 'x' }],
});
check('inlineMediaSources 收集 tool_runs + activity', inline.has('C:\\a.png') && inline.has('C:\\b.mp4'), [...inline].join(','));
check('旧消息（无 media）返回空集合', media.inlineMediaSources({ attachments: [{ source: 'C:\\old.png' }] }).size === 0);

// 6. 媒体判定读 bootstrap 名单（唯一定义）
check('mediaKind 图片', media.mediaKind('C:\\a.png') === 'image');
check('mediaKind 视频', media.mediaKind('C:\\a.m4v') === 'video');
check('mediaKind 非媒体', media.mediaKind('C:\\a.bmp') === 'other');

// 7. 末尾网格去重：新消息（媒体已就地）不重复渲染；旧消息照旧
const newMessage = {
  attachments: [{ source: 'C:\\a.png', name: 'a.png' }],
  activity: [{ type: 'tool', run: { media: [{ source: 'C:\\a.png', name: 'a.png' }] } }],
};
check('新消息末尾网格为空（不重复显示）', media.remainingAttachments(newMessage).length === 0, JSON.stringify(media.remainingAttachments(newMessage)));
const legacyMessage = { attachments: [{ path: 'C:\\old.png', name: 'old.png' }], tool_runs: [{ tool: 'read_file' }] };
check('旧消息末尾网格保留附件', media.remainingAttachments(legacyMessage).length === 1, JSON.stringify(media.remainingAttachments(legacyMessage)));
const mixed = {
  attachments: [{ source: 'C:\\a.png' }, { path: 'C:\\legacy.png' }],
  tool_runs: [{ media: [{ source: 'C:\\a.png' }] }],
};
check('混合：只留未就地的那份', media.remainingAttachments(mixed).length === 1, JSON.stringify(media.remainingAttachments(mixed)));

// 8. 用户气泡附件：音视频就地播放 + 文件名标签（D1）
const userVideo = media.uploadedFileMarkup([{ name: 'v.mp4', path: 'C:\\v.mp4' }]);
const userAudio = media.uploadedFileMarkup([{ name: 'a.mp3', path: 'C:\\a.mp3' }]);
const userImage = media.uploadedFileMarkup([{ name: 'i.png', path: 'C:\\i.png', thumb_path: 'C:\\i_thumb.webp' }]);
const userOther = media.uploadedFileMarkup([{ name: 'd.pdf', path: 'C:\\d.pdf' }]);
check('用户视频就地播放', userVideo.includes('<video') && userVideo.includes('controls'), userVideo);
check('用户音频就地播放', userAudio.includes('<audio') && userAudio.includes('controls'), userAudio);
check('用户视频带文件名标签', userVideo.includes('<figcaption title="v.mp4">v.mp4</figcaption>'), userVideo);
check('用户图片仍走缩略图 + 灯箱', userImage.includes('i_thumb.webp') && userImage.includes('data-large-url='), userImage);
check('用户非媒体仍是文件 chip', userOther.includes('class="file-chip"') && !userOther.includes('<video'), userOther);
check('用户非媒体 chip 可点开（href 指向 /api/file）', userOther.includes('href="/api/file?token=TOKEN'), userOther);

// 9. 助手侧媒体文件名标签（D6）
const assistantImage = media.mediaMarkup([{ kind: 'image', name: 'a.png', source: 'C:\\a.png' }]);
const assistantVideo = media.mediaMarkup([{ kind: 'video', name: 'v.mp4', source: 'C:\\v.mp4' }]);
const assistantAudio = media.mediaMarkup([{ kind: 'audio', name: 'a.mp3', source: 'C:\\a.mp3' }]);
check('助手图片带 figcaption', assistantImage.includes('<figcaption title="a.png">a.png</figcaption>'), assistantImage);
check('助手视频带 figcaption', assistantVideo.includes('<figcaption title="v.mp4">v.mp4</figcaption>'), assistantVideo);
check('助手音频带 figcaption', assistantAudio.includes('<figcaption title="a.mp3">a.mp3</figcaption>'), assistantAudio);

// 10. 活动时间线只渲染最后一段正文（工具调用前的过程播报不得堆满一条回复，§九.97）
// 这是**真执行**渲染函数：源码级断言只能证明"代码里有过滤"，证明不了"渲染结果对"。
const noisy = [
  { type: 'prose', text: '我已核对接口和调用链。', request_index: 1 },
  { type: 'tool', run: { tool: 'read_file', success: true }, request_index: 1 },
  { type: 'prose', text: '根因已经锁定。', request_index: 2 },
  { type: 'tool', run: { tool: 'edit_file', success: true }, request_index: 2 },
  { type: 'prose', text: '现在补协议。', request_index: 3 },
  { type: 'tool', run: { tool: 'pwsh', success: true }, request_index: 3 },
  { type: 'prose', text: '修好了，共改 2 个文件。', request_index: 4 },
];
const noisyHtml = media.activityMarkup(noisy);
const proseCount = (html) => (String(html).match(/class="stream-prose/g) || []).length;
check('三句过程播报只渲染最后一段正文', proseCount(noisyHtml) === 1, String(proseCount(noisyHtml)));
check(
  '渲染的是最终答复、且不残留任何中途播报',
  noisyHtml.includes('修好了，共改 2 个文件。')
    && !noisyHtml.includes('我已核对接口和调用链。')
    && !noisyHtml.includes('根因已经锁定。')
    && !noisyHtml.includes('现在补协议。'),
  noisyHtml,
);
check('工具条目一个不少（3 个）', (noisyHtml.match(/class="tool-run/g) || []).length === 3, noisyHtml);
check(
  '单段正文照常渲染',
  proseCount(media.activityMarkup([
    { type: 'tool', run: { tool: 'read_file', success: true } },
    { type: 'prose', text: '只有一句话。' },
  ])) === 1,
);
check(
  '只有工具时没有正文块',
  proseCount(media.activityMarkup([{ type: 'tool', run: { tool: 'read_file', success: true } }])) === 0,
);
check(
  '思考条目不被正文过滤牵连',
  media.activityMarkup([
    { type: 'reasoning', text: '第一段思考', request_index: 1 },
    { type: 'prose', text: '播报', request_index: 1 },
    { type: 'tool', run: { tool: 'read_file', success: true }, request_index: 1 },
    { type: 'prose', text: '最终答复。', request_index: 2 },
  ]).includes('reasoning-block'),
);

console.log();
console.log(`媒体渲染校验：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
process.exit(failures.length ? 1 : 0);
