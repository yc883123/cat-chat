// ============================================================
// 03-media.js —— 拆分自 public/app.js 第 712-1211 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, draggedFileCache, escapeHtml, localFileUrl, state, toast } from "./01-core.js";
import { markdown } from "./02-markdown.js";
import { selectedProvider } from "./07-models-agents.js";
import { renderPendingFiles } from "./10-upload.js";
// 输入框自增高（§九.128）：placeholder 与输入区宽度都在 updateContextComposerLock 里变，
// 必须在那里同步重算一次高度（13-skill-refs 只依赖 01-core/02-markdown，不引入新的成环面）。
import { resizeTextarea } from "./13-skill-refs.js";
export function fileUrl(source) {
  const value = String(source || '');
  if (/^https?:\/\//i.test(value) && !/^https?:\/\/(?:127\.0\.0\.1|localhost):8188\//i.test(value)) return value;
  // 本地路径的 URL 形状唯一定义在 01-core.localFileUrl（聊天背景图也用它，
  // 避免两处各拼一份 /api/file URL 后慢慢漂移）。
  return localFileUrl(value);
}

export function attachmentThumbPath(attachment) {
  if (attachment.thumb_path) return attachment.thumb_path;
  const p = String(attachment.path || attachment.source || '');
  if (!p || /^https?:\/\//i.test(p)) return '';
  const dot = p.lastIndexOf('.');
  return (dot > 0 ? p.slice(0, dot) : p) + '_thumb.webp';
}

export function attachmentThumbUrl(attachment) {
  const thumb = attachmentThumbPath(attachment);
  const source = attachment.path || attachment.source || '';
  return thumb ? fileUrl(thumb) : fileUrl(source);
}

// 多媒体类型判定（唯一入口）：扩展名名单来自后端 /api/bootstrap.media_exts
// （唯一定义 naiba/core/media_types.py），前端不再各写一份正则——两处漂移曾导致
// `.bmp`/`.svg` 产物既无"修改文件"chip、也无媒体卡，在消息里彻底消失。
// 返回 'image' | 'video' | 'audio' | 'other'。
export function mediaKind(source, name = '') {
  const lists = state.bootstrap?.media_exts?.exts || {};
  for (const candidate of [source, name]) {
    const ext = fileExtension(candidate);
    if (!ext) continue;
    for (const kind of ['image', 'video', 'audio']) {
      if ((lists[kind] || []).includes(ext)) return kind;
    }
  }
  return 'other';
}

function fileExtension(value) {
  const text = String(value || '').split('?')[0];
  const dot = text.lastIndexOf('.');
  if (dot <= 0) return '';
  const ext = text.slice(dot).toLowerCase();
  return /^\.[a-z0-9]+$/.test(ext) ? ext : '';
}

// ---- 大图灯箱：会话内左右切换 ----
// 图片列表 = 当前会话消息里所有可放大的图片（#messages img[data-large-url]），
// 按 DOM 顺序（= 历史出现顺序）去重；输入区待发送附件与右侧文件面板的图片不参与切换。
let lightboxItems = [];
let lightboxIndex = -1;

function collectConversationImages() {
  const container = $('#messages');
  if (!container) return [];
  const seen = new Set();
  const items = [];
  container.querySelectorAll('img[data-large-url]').forEach((img) => {
    const url = String(img.getAttribute('data-large-url') || '');
    if (!url || seen.has(url)) return;
    seen.add(url);
    items.push({ url, name: String(img.getAttribute('alt') || '') });
  });
  return items;
}

function renderLightboxFrame() {
  const img = $('#imageLightboxImg');
  const prev = $('#imageLightboxPrev');
  const next = $('#imageLightboxNext');
  const counter = $('#imageLightboxCounter');
  const item = lightboxItems[lightboxIndex];
  if (!img || !item) return;
  img.src = item.url;
  img.alt = item.name || '大图预览';
  const multiple = lightboxItems.length > 1;
  if (prev) prev.hidden = !multiple;
  if (next) next.hidden = !multiple;
  if (counter) {
    counter.hidden = !multiple;
    counter.textContent = multiple ? `${lightboxIndex + 1} / ${lightboxItems.length}` : '';
  }
  // 换图后回到适配尺寸（避免沿用上一张的缩放/平移）。
  resetLightboxZoom();
}

export function openImageLightbox(largeUrl, sourceEl = null) {
  const img = $('#imageLightboxImg');
  const box = $('#imageLightbox');
  const url = String(largeUrl || '');
  if (!img || !box || !url) return;
  if (!/^(\/api\/file|https?:\/\/)/i.test(url)) return;
  const inConversation = Boolean(sourceEl && sourceEl.closest && sourceEl.closest('#messages'));
  lightboxItems = inConversation ? collectConversationImages() : [];
  lightboxIndex = lightboxItems.findIndex((item) => item.url === url);
  if (lightboxIndex < 0) {
    // 不在会话列表内（输入区附件、文件面板）或列表为空：按单张展示，不显示左右按钮。
    lightboxItems = [{ url, name: String(sourceEl?.getAttribute?.('alt') || '') }];
    lightboxIndex = 0;
  }
  // 大图也不在磁盘上（例如被缓存清理后）：不能静默关闭——用户点了缩略图却什么都没发生，
  // 只会以为"预览坏了"。明确告知文件已不存在（缩略图那侧另有降级为文件图标的兜底）。
  img.onerror = () => {
    closeImageLightbox();
    toast('图片文件已不存在，可能已被缓存清理或移动');
  };
  box.hidden = false;
  box.setAttribute('aria-hidden', 'false');
  renderLightboxFrame();
  box.focus?.();
}

export function closeImageLightbox() {
  const box = $('#imageLightbox');
  if (box) {
    box.hidden = true;
    box.setAttribute('aria-hidden', 'true');
    box.classList.remove('is-zoomed', 'dragging');
  }
  const img = $('#imageLightboxImg');
  if (img) {
    img.removeAttribute('src');
    img.style.transform = '';
    img.classList.remove('zoomed');
  }
  lightboxItems = [];
  lightboxIndex = -1;
  lightboxScale = 1;
  lightboxTx = 0;
  lightboxTy = 0;
  lightboxDrag = null;
  lightboxTouch = null;
}

// 左右切换（delta = ±1）；返回 false 表示当前没有可切换的列表。
export function stepImageLightbox(delta) {
  if (lightboxItems.length < 2 || lightboxIndex < 0) return false;
  const count = lightboxItems.length;
  lightboxIndex = (lightboxIndex + delta + count) % count;
  renderLightboxFrame();
  return true;
}

// 灯箱打开时消费 ←/→/Esc；返回 true 表示按键已被处理。
export function handleImageLightboxKey(event) {
  const box = $('#imageLightbox');
  if (!box || box.hidden) return false;
  if (event.key === 'ArrowLeft') { event.preventDefault(); stepImageLightbox(-1); return true; }
  if (event.key === 'ArrowRight') { event.preventDefault(); stepImageLightbox(1); return true; }
  if (event.key === 'Escape') { event.preventDefault(); closeImageLightbox(); return true; }
  return false;
}

// ---- 灯箱缩放 / 拖动 / 半屏点击翻页 ----
// 规则：未缩放时点左半屏=上一张、右半屏=下一张（单张图时点空白处仍是关闭）；
// 缩放后单击不翻页（让位给拖动），双击复位，滚轮/双指捏合缩放，拖动平移。
const LIGHTBOX_MAX_SCALE = 6;
let lightboxScale = 1;
let lightboxTx = 0;
let lightboxTy = 0;
let lightboxDrag = null;          // { x, y, tx, ty, moved, pointer }
let lightboxTouch = null;         // { mode: 'pan'|'pinch', ... }
let lightboxSuppressClick = false; // 拖动/双指结束后抑制随后的 click
let lightboxLastTapAt = 0;

function applyLightboxTransform() {
  const img = $('#imageLightboxImg');
  const box = $('#imageLightbox');
  if (!img) return;
  if (lightboxScale <= 1.001) {
    lightboxScale = 1;
    lightboxTx = 0;
    lightboxTy = 0;
    img.style.transform = '';
    img.classList.remove('zoomed');
  } else {
    img.style.transform = `translate(${lightboxTx}px, ${lightboxTy}px) scale(${lightboxScale})`;
    img.classList.add('zoomed');
  }
  box?.classList.toggle('is-zoomed', lightboxScale > 1.001);
}

export function resetLightboxZoom() {
  lightboxScale = 1;
  lightboxTx = 0;
  lightboxTy = 0;
  applyLightboxTransform();
}

function clampLightboxPan() {
  const img = $('#imageLightboxImg');
  if (!img || lightboxScale <= 1.001) {
    lightboxTx = 0;
    lightboxTy = 0;
    return;
  }
  const rect = img.getBoundingClientRect();
  const layoutW = rect.width / lightboxScale;
  const layoutH = rect.height / lightboxScale;
  // 只在图片比视口大时才允许平移，避免把图拖出屏幕。
  const maxX = Math.max(0, (layoutW * lightboxScale - window.innerWidth) / 2);
  const maxY = Math.max(0, (layoutH * lightboxScale - window.innerHeight) / 2);
  lightboxTx = Math.max(-maxX, Math.min(maxX, lightboxTx));
  lightboxTy = Math.max(-maxY, Math.min(maxY, lightboxTy));
}

// 以 (clientX, clientY) 为锚点缩放：保持光标下的图像点不动。
function zoomLightboxAt(nextScale, clientX, clientY) {
  const img = $('#imageLightboxImg');
  if (!img) return;
  const target = Math.max(1, Math.min(LIGHTBOX_MAX_SCALE, nextScale));
  if (Math.abs(target - lightboxScale) < 0.001) return;
  const rect = img.getBoundingClientRect();
  const centerX = rect.left + rect.width / 2 - lightboxTx;
  const centerY = rect.top + rect.height / 2 - lightboxTy;
  const vx = clientX - centerX;
  const vy = clientY - centerY;
  const ratio = target / lightboxScale;
  lightboxTx = vx - (vx - lightboxTx) * ratio;
  lightboxTy = vy - (vy - lightboxTy) * ratio;
  lightboxScale = target;
  clampLightboxPan();
  applyLightboxTransform();
}

function toggleLightboxZoom(clientX, clientY) {
  if (lightboxScale > 1.001) {
    resetLightboxZoom();
    return;
  }
  const rect = $('#imageLightboxImg')?.getBoundingClientRect();
  zoomLightboxAt(2.5, clientX ?? (rect ? rect.left + rect.width / 2 : window.innerWidth / 2), clientY ?? window.innerHeight / 2);
}

function touchDistance(touches) {
  const dx = touches[0].clientX - touches[1].clientX;
  const dy = touches[0].clientY - touches[1].clientY;
  return Math.hypot(dx, dy);
}

export function initImageLightboxInteractions() {
  const box = $('#imageLightbox');
  const img = $('#imageLightboxImg');
  if (!box || !img) return;

  // 滚轮缩放（以光标为锚点）
  box.addEventListener('wheel', (event) => {
    if (box.hidden) return;
    event.preventDefault();
    zoomLightboxAt(lightboxScale * (event.deltaY < 0 ? 1.15 : 1 / 1.15), event.clientX, event.clientY);
  }, { passive: false });

  // 鼠标拖动平移（仅缩放后）
  box.addEventListener('mousedown', (event) => {
    if (event.button !== 0 || lightboxScale <= 1.001) return;
    if (event.target.closest?.('button')) return;
    lightboxDrag = { x: event.clientX, y: event.clientY, tx: lightboxTx, ty: lightboxTy, moved: false };
    box.classList.add('dragging');
    event.preventDefault();
  });
  document.addEventListener('mousemove', (event) => {
    if (!lightboxDrag) return;
    const dx = event.clientX - lightboxDrag.x;
    const dy = event.clientY - lightboxDrag.y;
    if (!lightboxDrag.moved && Math.hypot(dx, dy) > 4) lightboxDrag.moved = true;
    if (!lightboxDrag.moved) return;
    lightboxTx = lightboxDrag.tx + dx;
    lightboxTy = lightboxDrag.ty + dy;
    clampLightboxPan();
    applyLightboxTransform();
  });
  document.addEventListener('mouseup', () => {
    if (!lightboxDrag) return;
    const moved = lightboxDrag.moved;
    lightboxDrag = null;
    box.classList.remove('dragging');
    if (moved) {
      // 拖动结束后的 click 不应触发翻页/关闭。
      lightboxSuppressClick = true;
      window.setTimeout(() => { lightboxSuppressClick = false; }, 0);
    }
  });

  // 单击：未缩放时按屏幕左右半屏翻页；单张图时点图片以外区域关闭（保持原行为）
  box.addEventListener('click', (event) => {
    if (event.target.closest?.('button')) return;
    if (lightboxSuppressClick) return;
    if (lightboxScale > 1.001) return;
    if (lightboxItems.length < 2) {
      if (event.target !== img) closeImageLightbox();
      return;
    }
    stepImageLightbox(event.clientX < window.innerWidth / 2 ? -1 : 1);
  });

  // 双击：缩放后复位
  box.addEventListener('dblclick', (event) => {
    if (event.target.closest?.('button')) return;
    if (lightboxScale > 1.001) resetLightboxZoom();
  });

  // 触屏：单指平移（缩放后）、双指捏合缩放、双击切换缩放
  box.addEventListener('touchstart', (event) => {
    if (box.hidden) return;
    if (event.touches.length >= 2) {
      lightboxTouch = {
        mode: 'pinch',
        distance: touchDistance(event.touches),
        scale: lightboxScale,
        x: (event.touches[0].clientX + event.touches[1].clientX) / 2,
        y: (event.touches[0].clientY + event.touches[1].clientY) / 2,
        tx: lightboxTx,
        ty: lightboxTy,
      };
      return;
    }
    const touch = event.touches[0];
    lightboxTouch = {
      mode: 'pan',
      x: touch.clientX,
      y: touch.clientY,
      tx: lightboxTx,
      ty: lightboxTy,
      moved: false,
    };
  }, { passive: true });
  box.addEventListener('touchmove', (event) => {
    if (!lightboxTouch) return;
    if (lightboxTouch.mode === 'pinch' && event.touches.length >= 2) {
      event.preventDefault();
      const next = lightboxTouch.scale * (touchDistance(event.touches) / (lightboxTouch.distance || 1));
      zoomLightboxAt(next, lightboxTouch.x, lightboxTouch.y);
      return;
    }
    if (lightboxTouch.mode === 'pan' && event.touches.length === 1 && lightboxScale > 1.001) {
      event.preventDefault();
      const touch = event.touches[0];
      const dx = touch.clientX - lightboxTouch.x;
      const dy = touch.clientY - lightboxTouch.y;
      if (!lightboxTouch.moved && Math.hypot(dx, dy) > 6) lightboxTouch.moved = true;
      if (!lightboxTouch.moved) return;
      lightboxTx = lightboxTouch.tx + dx;
      lightboxTy = lightboxTouch.ty + dy;
      clampLightboxPan();
      applyLightboxTransform();
    }
  }, { passive: false });
  box.addEventListener('touchend', (event) => {
    if (!lightboxTouch) return;
    const moved = Boolean(lightboxTouch.moved);
    const wasPinch = lightboxTouch.mode === 'pinch';
    const lastX = lightboxTouch.x;
    const lastY = lightboxTouch.y;
    lightboxTouch = null;
    if (moved || wasPinch) {
      lightboxSuppressClick = true;
      window.setTimeout(() => { lightboxSuppressClick = false; }, 0);
      return;
    }
    // 双击（双触）切换缩放
    const now = Date.now();
    if (now - lightboxLastTapAt < 300) {
      lightboxLastTapAt = 0;
      lightboxSuppressClick = true;
      window.setTimeout(() => { lightboxSuppressClick = false; }, 0);
      toggleLightboxZoom(lastX, lastY);
      return;
    }
    lightboxLastTapAt = now;
    if (event.touches.length === 0 && lightboxItems.length < 2 && lightboxScale <= 1.001) {
      closeImageLightbox();
    }
  });
}

// ---- 大图右键 → 复制图片到剪贴板 ----
// pywebview（WebView2）默认关闭了浏览器右键菜单（AreDefaultContextMenusEnabled 仅 debug 开启），
// 因此在 pywebview 窗口内自绘一个轻量菜单；真实浏览器保留其原生“复制图片”。
export function isPywebview() {
  return Boolean(window.pywebview && window.pywebview.api);
}

export function ensureImageContextMenu() {
  const menu = $('#imageContextMenu');
  if (menu) return menu;
  const m = document.createElement('div');
  m.className = 'image-context-menu';
  m.id = 'imageContextMenu';
  m.setAttribute('role', 'menu');
  m.setAttribute('aria-label', '图片操作');
  m.innerHTML = '<button type="button" role="menuitem" data-image-context-action="copy">复制图片</button>';
  m.hidden = true;
  document.body.append(m);
  return m;
}

export function hideImageContextMenu() {
  const menu = $('#imageContextMenu');
  if (menu) menu.hidden = true;
}

export function showImageContextMenu(event) {
  const menu = ensureImageContextMenu();
  menu.hidden = false;
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  menu.style.left = `${Math.max(6, Math.min(event.clientX, window.innerWidth - width - 6))}px`;
  menu.style.top = `${Math.max(6, Math.min(event.clientY, window.innerHeight - height - 6))}px`;
  // 不自动聚焦按钮，避免图片失焦影响后续复制路径。
}

export function bytesToBase64(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

// 取当前大图的字节。优先同源 fetch（/api/file 由其自身服务，必然可读）；
// 跨源或 fetch 失败时回退 canvas 转 PNG（跨源且未开 CORS 的图会被污染并抛错）。
export async function imageBytesFrom(img) {
  const url = img.currentSrc || img.src;
  if (url) {
    try {
      const resp = await fetch(url);
      if (resp.ok) {
        const blob = await resp.blob();
        const bytes = new Uint8Array(await blob.arrayBuffer());
        if (blob.type && blob.type.startsWith('image/')) {
          return { bytes, mime: blob.type };
        }
      }
    } catch (_) { /* 跨源或网络错误，走 canvas 回退 */ }
  }
  const canvas = document.createElement('canvas');
  canvas.width = img.naturalWidth || img.width;
  canvas.height = img.naturalHeight || img.height;
  if (!canvas.width || !canvas.height) throw new Error('图片尚未加载完成');
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  const dataUrl = canvas.toDataURL('image/png');
  const b64 = dataUrl.split(',')[1] || '';
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return { bytes, mime: 'image/png' };
}

export async function copyLightboxImage() {
  const img = $('#imageLightboxImg');
  if (!img) throw new Error('未找到图片');
  const { bytes, mime } = await imageBytesFrom(img);
  // 1) 原生剪贴板 API：127.0.0.1 / localhost 是安全上下文，WebView2 通常可用。
  if (navigator.clipboard?.write && window.ClipboardItem) {
    try {
      await navigator.clipboard.write([new ClipboardItem({ [mime]: new Blob([bytes], { type: mime }) })]);
      return 'clipboard';
    } catch (_) {
      // WebView 可能未授予剪贴板权限，回退到 Python 桥。
    }
  }
  // 2) pywebview 桥：把图片写入 Windows CF_DIB 剪贴板（桌面端最可靠）。
  if (window.pywebview?.api?.copy_image_to_clipboard) {
    const b64 = bytesToBase64(bytes);
    const result = await window.pywebview.api.copy_image_to_clipboard(b64);
    if (result && result.ok) return 'python';
    throw new Error((result && result.error) || '复制到剪贴板失败');
  }
  throw new Error('当前环境不支持复制图片');
}

export async function runImageContextAction(action) {
  try {
    if (action === 'copy') {
      const via = await copyLightboxImage();
      toast(via === 'python' ? '已复制图片到剪贴板' : '已复制图片');
    }
  } catch (error) {
    toast(`复制失败：${error.message}`);
  } finally {
    hideImageContextMenu();
  }
}

// 点击缩略图 → 弹大图；拖拽历史缩略图 → 以"大图 URL"拖动；缩略图 404 → 回退原图。
document.addEventListener('click', (event) => {
  const target = event.target.closest?.('[data-large-url]');
  if (target) openImageLightbox(target.getAttribute('data-large-url'), target);
});
document.addEventListener('dragstart', (event) => {
  const target = event.target.closest?.('[data-large-url]');
  if (!target) return;
  const url = target.getAttribute('data-large-url');
  if (!url) return;
  try { event.dataTransfer.effectAllowed = 'copy'; } catch (_) { /* ignore */ }
  try { event.dataTransfer.setData('text/uri-list', url); } catch (_) { /* ignore */ }
  try { event.dataTransfer.setData('text/plain', url); } catch (_) { /* ignore */ }
  // 解析大图 URL 里的真实文件路径：拖到输入框可复用该图片。
  let absoluteUrl = url;
  try {
    const parsed = new URL(url, location.href);
    absoluteUrl = parsed.href;
    const filePath = decodeURIComponent(parsed.searchParams.get('path') || '');
    if (filePath) event.dataTransfer.setData('application/x-naiba-file-path', filePath);
  } catch (_) { /* ignore */ }
  // 若已预取到该图字节，作为真实文件加入拖拽（拖到桌面另存、拖进输入框都生效）。
  try {
    const cached = draggedFileCache.get(absoluteUrl);
    if (cached && event.dataTransfer.items?.add) {
      event.dataTransfer.items.add(cached);
      event.dataTransfer.setData('DownloadURL', `${cached.type || 'application/octet-stream'}:${cached.name}:${absoluteUrl}`);
    }
  } catch (_) { /* ignore */ }
});
document.addEventListener('error', (event) => {
  const img = event.target;
  if (!(img && String(img.tagName).toUpperCase() === 'IMG' && img.classList.contains('thumbnail') && !img.dataset.fallback)) return;
  img.dataset.fallback = '1';
  const large = img.getAttribute('data-large-url');
  if (large && img.getAttribute('src') !== large) img.src = large;
}, true);

// 点击缩略图上的「发送到输入框」按钮：把该图加入待发送附件。
// 不依赖 HTML5 拖拽，因此在内嵌 webview 窗口里同样可用。
document.addEventListener('click', (event) => {
  const reuse = event.target.closest?.('[data-reuse-source]');
  if (!reuse) return;
  event.stopPropagation();
  let source = reuse.getAttribute('data-reuse-source') || '';
  const name = reuse.getAttribute('data-reuse-name') || 'image';
  const thumb = reuse.getAttribute('data-reuse-thumb') || '';
  if (!source) return;
  // 若是 /api/file URL，解码出真实文件路径；否则直接使用绝对路径。
  try {
    const parsed = new URL(source, location.href);
    if (parsed.pathname === '/api/file') source = decodeURIComponent(parsed.searchParams.get('path') || source);
  } catch (_) { /* keep source */ }
  if (state.pendingFiles.some((file) => file.path === source)) return;
  const chip = { name: String(source).split(/[\\/]/).pop() || name, path: source, size: 0 };
  if (thumb) chip.thumb_path = thumb;
  state.pendingFiles.push(chip);
  renderPendingFiles();
  const ta = $('#composerTextarea') || document.querySelector('.composer textarea, .composer input');
  if (ta) ta.focus();
});

// 用户气泡/编辑框里的附件渲染（与助手侧同口径的媒体判定）：图片带缩略图+灯箱，
// 视频/音频就地播放，其它类型仍是文件名 chip。放在本模块便于渲染守门真执行校验。
export function uploadedFileMarkup(files = []) {
  if (!files.length) return '';
  const html = files.map((file) => {
    const source = file.source || file.path || '';
    const kind = mediaKind(source, file.name);
    const url = escapeHtml(fileUrl(source));
    const name = escapeHtml(file.name || '');
    if (kind === 'image') {
      const thumbUrl = attachmentThumbUrl(file);
      return `<figure class="attachment attachment-image"><img class="thumbnail" src="${escapeHtml(thumbUrl)}" alt="${name}" loading="lazy" draggable="true" data-large-url="${url}"><figcaption title="${name}">${name}</figcaption></figure>`;
    }
    if (kind === 'video') return `<figure class="attachment attachment-media"><video src="${url}" controls playsinline preload="metadata"></video><figcaption title="${name}">${name}</figcaption></figure>`;
    if (kind === 'audio') return `<figure class="attachment attachment-media"><audio src="${url}" controls preload="metadata"></audio><figcaption title="${name}">${name}</figcaption></figure>`;
    // 非媒体（pdf/doc/zip…）：文件名 chip 直接可点开（浏览器能预览的预览、否则下载），
    // 与助手侧 mediaMarkup 的 chip 同口径——不再只是"看得见、点不动"的死文本。
    return `<a class="file-chip" href="${url}" target="_blank" rel="noreferrer">${name}</a>`;
  }).join('');
  return `<div class="media-grid">${html}</div>`;
}

// 气泡里默认露出的文件夹条目数：再多就刷屏了（完整清单在 `<details>` 里，上限 300 条）。
export const FOLDER_INDEX_PREVIEW = 10;

// 用户气泡里的文件夹索引：**看得见，但只露个头**。
// 完整清单（上限 300 条）同时进了模型载荷，那是 metadata 的事；气泡把 300 行路径全铺出来
// 只会把对话框变成文件列表。默认只列前 10 条 + 一行「另有 N 项」，点摘要才展开剩下的。
// 用原生 <details> 而不是自绘开合：消息重渲染（懒加载、流式收尾）会把自绘的展开状态冲掉，
// 原生元素的状态跟着 DOM 一起重建，不需要在任何地方维护。
export function folderIndexMarkup(indexes = []) {
  const items = (Array.isArray(indexes) ? indexes : []).filter((item) => item && item.path);
  if (!items.length) return '';
  const blocks = items.map((item) => {
    const entries = Array.isArray(item.entries) ? item.entries : [];
    const total = Number(item.total ?? entries.length) || 0;
    const images = Number(item.image_count || 0);
    const preview = entries.slice(0, FOLDER_INDEX_PREVIEW);
    const rest = entries.slice(FOLDER_INDEX_PREVIEW);
    const hidden = Math.max(0, total - preview.length);
    const lines = (list) => list
      .map((entry) => `<li>${escapeHtml(String(entry?.rel || ''))}</li>`)
      .join('');
    const hint = hidden
      ? `<li class="folder-index-more">另有 ${hidden} 项未显示${rest.length ? '（点开查看）' : ''}</li>`
      : '';
    const body = rest.length
      ? `<ul class="folder-index-list">${lines(rest)}`
        + (item.truncated
          ? `<li class="folder-index-more">…仅列出前 ${entries.length} 项，另有 ${Math.max(0, total - entries.length)} 项未列出（可用工具按路径读取）</li>`
          : '')
        + '</ul>'
      : '';
    const name = escapeHtml(String(item.name || '文件夹'));
    const path = escapeHtml(String(item.path));
    return `<details class="folder-index">
      <summary><span class="folder-index-head">📁 ${name} · 共 ${total} 项（含 ${images} 图）</span>
      <ul class="folder-index-preview">${lines(preview)}${hint}</ul></summary>
      ${body}
      <span class="folder-index-path" title="${path}">${path}</span>
    </details>`;
  }).join('');
  return `<div class="folder-indexes">${blocks}</div>`;
}

export function mediaMarkup(attachments = []) {
  if (!attachments.length) return '';
  const items = attachments.map((attachment) => {
    const source = attachment.source || attachment.path;
    const kind = mediaKind(source, attachment.name);
    const url = fileUrl(source);
    const safeUrl = escapeHtml(url);
    const name = escapeHtml(attachment.name || '生成文件');
    // 每种媒体都带文件名标签（figcaption）：气泡里的图/视频/音频不再是无名之物，
    // 也便于与"修改文件"或工具块里的路径对应。
    if (kind === 'image') {
      const thumbUrl = attachmentThumbUrl(attachment);
      const reusePath = attachment.source || attachment.path || '';
      const reuseThumb = attachment.thumb_path || '';
      return `<figure class="media-item"><img class="media-image thumbnail" src="${escapeHtml(thumbUrl)}" alt="${name}" loading="lazy" draggable="true" data-large-url="${safeUrl}"><button class="thumb-reuse" type="button" title="发送到输入框（复用此图）" aria-label="发送到输入框" data-reuse-source="${escapeHtml(reusePath)}" data-reuse-name="${name}" data-reuse-thumb="${escapeHtml(reuseThumb)}">↩</button><figcaption title="${name}">${name}</figcaption></figure>`;
    }
    if (kind === 'video') return `<figure class="media-item"><video src="${safeUrl}" controls playsinline preload="metadata"></video><figcaption title="${name}">${name}</figcaption></figure>`;
    if (kind === 'audio') return `<figure class="media-item"><audio src="${safeUrl}" controls preload="metadata"></audio><figcaption title="${name}">${name}</figcaption></figure>`;
    return `<a class="file-chip" href="${safeUrl}" target="_blank" rel="noreferrer">${name}</a>`;
  }).join('');
  return `<div class="media-grid">${items}</div>`;
}

export function toolRunMarkup(run = {}, markerClass = '') {
  // 工具块下方就地内嵌本次调用产出的媒体（P3）：媒体记录来自后端在产出点按声明
  // 采集的 run.media，流式与历史重放走同一份数据，位置天然一致。
  return `<details class="tool-run${markerClass}">
    <summary>${run.success ? '已执行' : '执行失败'} · ${escapeHtml(run.tool)}${run.reason ? ` · ${escapeHtml(run.reason)}` : ''}</summary>
    <pre>${escapeHtml(JSON.stringify(run.arguments || {}, null, 2))}\n\n${escapeHtml(run.result || '')}</pre>
  </details>${toolMediaMarkup(run)}`;
}

// 分桶截断的自述信息（后端 attachments_truncated / media_truncated 同构）：
// "共 N 个媒体，仅显示前 M 个"——静默截断 = 误导源，必须让用户看见。
export function mediaTruncatedNotice(truncated) {
  if (!truncated || typeof truncated !== 'object') return '';
  const total = Number(truncated.total || 0);
  const shown = Number(truncated.shown || 0);
  if (!(total > 0) || shown >= total) return '';
  return `<div class="media-truncated">共 ${total} 个媒体，仅显示前 ${shown} 个</div>`;
}

// 本轮答复的截断自述（后端 metadata.truncated）：模型撞输出上限、或流在没有终止原因的
// 情况下带着"明显没说完"的正文结束时写入——静默截断 = 误导源，必须让用户看见。
export function truncationNotice(truncated) {
  if (!truncated || typeof truncated !== 'object' || !truncated.truncated) return '';
  const reason = String(truncated.finish_reason || '');
  // cause 由后端区分同一条 length 的两种成因（本地模型 prompt+completion 贴到窗口 =
  // context，其余 = output）。两者对用户的建议相反：撞输出上限可以调大输出上限/让它接着写，
  // 窗口耗尽只能新会话——曾经两者都显示「已达模型输出上限」，用户照着续写只会越写越失败。
  const cause = String(truncated.cause || '');
  let detail = '未收到终止原因（流可能被上游掐断）';
  if (cause === 'context') detail = '上下文窗口已耗尽，建议「新会话」后继续';
  else if (reason === 'length') detail = '已达模型输出上限';
  else if (reason) detail = `终止原因 ${reason}`;
  const parts = [detail];
  if (truncated.continued) parts.push('已自动续写一次');
  if (truncated.unfinished_tail) parts.push('正文停在未完成的标点');
  return `<div class="truncation-notice" title="模型这一轮没有正常收尾，正文可能不完整">本轮回复可能被截断：${escapeHtml(parts.join(' · '))}</div>`;
}

// 单次工具调用的媒体块（就地内嵌）：无媒体且无截断提示时返回空串。
export function toolMediaMarkup(run = {}) {
  const media = Array.isArray(run.media) ? run.media : [];
  const notice = mediaTruncatedNotice(run.media_truncated);
  if (!media.length && !notice) return '';
  return `<div class="tool-media">${media.length ? mediaMarkup(media) : ''}${notice}</div>`;
}

// 消息里"已就地渲染"的媒体来源集合（末尾网格据此去重，避免同一张图出现两遍）。
// 新消息的媒体挂在 activity/tool_runs 的 run.media 上；旧会话没有该字段 → 集合为空，
// 末尾网格照旧渲染（双路径兼容，零数据迁移）。
export function inlineMediaSources(metadata = {}) {
  const sources = new Set();
  const addRun = (run) => {
    if (!run || !Array.isArray(run.media)) return;
    run.media.forEach((item) => {
      const key = String((item && (item.source || item.path)) || '');
      if (key) sources.add(key);
    });
  };
  (Array.isArray(metadata.tool_runs) ? metadata.tool_runs : []).forEach(addRun);
  (Array.isArray(metadata.activity) ? metadata.activity : []).forEach((item) => {
    if (item && item.type === 'tool') addRun(item.run);
  });
  return sources;
}

// 末尾网格只渲染"没有就地归属"的附件：新消息的媒体都在 run.media 里（已就地渲染），
// 旧会话没有 run.media（集合为空 → 全部保留，末尾网格照旧）。两条渲染路径互不打架。
export function remainingAttachments(metadata = {}) {
  const inline = inlineMediaSources(metadata);
  const list = Array.isArray(metadata.attachments) ? metadata.attachments : [];
  return list.filter((attachment) => !inline.has(String((attachment && (attachment.source || attachment.path)) || '')));
}

export function toolMarkup(runs = []) {
  if (!runs.length) return '';
  return `<div class="tool-stack">${runs.map((run) => toolRunMarkup(run)).join('')}</div>`;
}

export function activityMarkup(activity = []) {
  if (!Array.isArray(activity) || !activity.length) return '';
  // 只渲染**最后一段正文**（prose）：更早的每一段都跟在某次工具调用前面，是模型"调用工具前的
  // 过程播报"（"我已定位根因 / 继续验证 / 现在补测试"），一轮能攒十来段近义废话，摞满一条回复
  // （见维护说明 §九.97）。后端出参已按同口径过滤（naiba/run/stream._build_activity_timeline），
  // 这里再兜一次是为**历史消息**——它们的 activity 早已带着那十来段落库，不重算就永远显示旧形态。
  const lastProseIndex = activity.reduce(
    (found, item, index) => ((item && item.type === 'prose') ? index : found), -1);
  const visible = lastProseIndex < 0
    ? activity
    : activity.filter((item, index) => (!item || item.type !== 'prose' || index === lastProseIndex));
  // 所有思考块一视同仁（均折叠）；请求轮次断点「·」标在每次新请求开始块的左侧
  // （request_index 由后端以 usage 事件为边界标注，与用量明细的请求序号对应）。
  let html = '';
  let prevRequestIndex = 0;
  visible.forEach((item, index) => {
    try {
      const requestStart = Number(item.request_index || 0) !== Number(prevRequestIndex || 0);
      const markerClass = requestStart ? ' request-start' : '';
      if (requestStart) prevRequestIndex = Number(item.request_index || 0);
      if (item.type === 'reasoning') html += reasoningMarkup([item.text], markerClass);
      else if (item.type === 'tool' && item.run) html += toolRunMarkup(item.run, markerClass);
      else if (item.type === 'prose') html += `<div class="stream-prose${markerClass}">${markdown(item.text)}</div>`;
    } catch (_) { /* 单个条目异常不影响整体 */ }
  });
  return html;
}

export function reasoningMarkup(reasoning, markerClass = '') {
  const list = Array.isArray(reasoning) ? reasoning.filter(Boolean) : (reasoning ? [reasoning] : []);
  if (!list.length) return '';
  // 每个思考段单独一行（可折叠）；一律折叠（不再有"最后一个思考块默认展开"的
  // 区别对待——用户实测确认展开态会造成最终答复被夹在时间线中间的观感问题）。
  return list.map((text) => {
    const clean = String(text || '').trim();
    const preview = clean.replace(/\s+/g, ' ').slice(0, 80);
    const summary = preview ? `思考：${preview}${clean.length > preview.length ? '…' : ''}` : '思考';
    const body = `<summary>${escapeHtml(summary)}</summary><div class="reasoning-content">${markdown(clean)}</div></details>`;
    return `<details class="reasoning-block tool-reasoning${markerClass}">${body}`;
  }).join('');
}

export function formatDateTime(ms) {
  const date = new Date(Number(ms) || 0);
  if (!date.getTime()) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

// 生成速率（token/s）：输出 tokens ÷ 请求耗时秒。
// 没有耗时的旧数据返回 null（不显示该字段）；耗时存在但输出为 0 时返回 0（如实显示）。
function requestTokenSpeed(item) {
  const ms = Number(item?.request_ms || 0);
  if (!(ms > 0)) return null;
  const output = Math.max(0, Number(item?.output_tokens || 0));
  return output / (ms / 1000);
}

// 本轮平均速率：按请求明细的「输出总量 ÷ 请求总耗时」加权（而不是各次速率的算术平均），
// 无可用明细（旧数据）时返回 null。
function averageTokenSpeed(details) {
  const rows = (Array.isArray(details) ? details : []).filter((item) => Number(item?.request_ms || 0) > 0);
  if (!rows.length) return null;
  const output = rows.reduce((sum, item) => sum + Math.max(0, Number(item.output_tokens || 0)), 0);
  const ms = rows.reduce((sum, item) => sum + Number(item.request_ms || 0), 0);
  return ms > 0 ? output / (ms / 1000) : null;
}

// %4d 打印：右对齐补空格到 4 位（配合 .n-speed 的 white-space: pre 保留空格）。
function formatSpeedPadded(rate) {
  return String(Math.round(rate)).padStart(4, ' ');
}

function usageRequestLine(item) {
  const input = Math.max(0, Number(item.input_tokens || 0));
  const output = Math.max(0, Number(item.output_tokens || 0));
  const cached = Math.max(0, Number(item.cached_tokens || 0));
  const total = Math.max(0, Number(item.total_tokens || 0)) || input + output;
  const rate = input ? (cached / input * 100).toFixed(1) : '0.0';
  const ms = Number(item.request_ms || 0);
  const speed = requestTokenSpeed(item);
  // 数值列对齐：token %6d / 时长 %3.1f / 命中率 %2.1f / 速率 %4d（CSS 定宽右对齐，无需千分位）。
  const speedHtml = speed === null ? '' : ` · 速率 <span class="n-speed">${formatSpeedPadded(speed)}</span> token/s`;
  return `<div class="usage-request-line">第 ${Number(item.index || 0)} 次请求：输入 <span class="n-tok">${input}</span> · 输出 <span class="n-tok">${output}</span> · 总 <span class="n-tok">${total}</span> · 命中率 <span class="n-rate">${rate}%</span>（命中 <span class="n-tok">${cached}</span> / 重算 <span class="n-tok">${Math.max(0, input - cached)}</span>）${speedHtml}${ms > 0 ? ` · 耗时 <span class="n-sec">${(ms / 1000).toFixed(1)}</span>s` : ''}</div>`;
}

export function usageMarkup(usage, createdAt = null) {
  if (!usage || typeof usage !== 'object') return '';
  const input = Number(usage.input_tokens || 0);
  const output = Number(usage.output_tokens || 0);
  const cached = Number(usage.cached_tokens || 0);
  const total = Number(usage.total_tokens || input + output);
  const performance = usage.performance || {};
  const vision = performance.vision || usage.lanes?.vision || {};
  const visualMs = Number(vision.total_ms || vision.diagnostics?.total_ms || 0);
  const visionCacheHit = Boolean(vision.cache_hit);
  if (!input && !output && !visualMs && !visionCacheHit) {
    return '<div class="usage-line">Token / 缓存命中率：供应商未返回</div>';
  }
  const rate = input ? Number(usage.cache_hit_rate ?? (cached / input * 100)).toFixed(1) : '0.0';
  const requests = Math.max(1, Number(usage.requests || 1));
  const miss = Math.max(0, Number(usage.uncached_tokens ?? (input - cached)));
  const details = Array.isArray(usage.requests_detail) ? usage.requests_detail : [];
  const detailsHtml = details.length
    ? `<button class="usage-toggle-btn" type="button" data-usage-toggle aria-expanded="false" aria-label="查看逐次请求明细">请求明细 <span class="usage-toggle-arrow">▸</span></button><div class="usage-requests" hidden>${details.map((item) => usageRequestLine(item)).join('')}</div>`
    : '';
  // 顶部总结行保持自然文本（不定宽对齐——定宽会让单行数字稀疏）；请求明细表内保留列格式化。
  const tokenLine = (input || output)
    ? `<div class="usage-line" title="本轮 ${requests} 次模型请求">本轮 ${total} tokens · 输入 ${input} · 输出 ${output} · 缓存命中率 ${rate}%（命中 ${cached} / 重算 ${miss}）${detailsHtml}</div>`
    : '';
  const durationMs = Number(performance.total_ms || 0);
  const elapsedMs = Number(usage.elapsed_ms || 0);
  const when = formatDateTime(createdAt);
  // 进行中（流式 usage 事件带 elapsed_ms、无终态 total_ms）显示"本轮已耗时"；
  // 完成后（终态 metadata.usage）显示汇总"本轮总耗时 + 完成日期"。
  const durationValue = durationMs > 0 ? durationMs : elapsedMs;
  const durationLabel = durationValue > 0
    ? `本轮${durationMs > 0 ? '总' : '已'}耗时 ${(durationValue / 1000).toFixed(1)}s`
    : '';
  // 平均速率（输出 tokens ÷ 请求总耗时）：有逐次明细时显示，旧数据无耗时则不显示。
  const avgSpeed = averageTokenSpeed(details);
  const speedLabel = avgSpeed === null ? '' : `平均 ${Math.round(avgSpeed)} token/s，`;
  const durationLine = durationLabel
    ? `<div class="usage-line usage-duration">${durationLabel}，${speedLabel}共 ${requests} 次请求${durationMs > 0 && when ? `。${when}` : ''}</div>`
    : '';
  // 只保留视觉 lane（聊天"lane 耗时"是最后一次请求的诊断值，与"本轮总耗时"重复且易误导，已移除）。
  const laneLine = (visualMs || visionCacheHit)
    ? `<div class="usage-line usage-performance">${visionCacheHit ? '视觉缓存命中' : ''}${(visionCacheHit && visualMs) ? ' · ' : ''}${visualMs ? `视觉 ${(visualMs / 1000).toFixed(1)}s` : ''}</div>`
    : '';
  const warnings = Array.isArray(performance.warnings) ? performance.warnings : [];
  const warningLine = warnings.map((item) => `<div class="usage-warning">${escapeHtml(item)}</div>`).join('');
  return `${tokenLine}${durationLine}${laneLine}${warningLine}`;
}

// 「新会话」分割线判据（与 04-messages.sessionDividerAfter 同一份口径）：
// 新形态 = 标记落在锚点消息的 metadata 上（`session_start`，分割线画在该消息**下方**）；
// 遗留形态 = 独立的 role=session 标记行（早期版本，那一行本身就是分割线）。
// 语义：此线以上的消息不再进入模型上下文（**含被标记的那条消息本身**）。
function isContextStartDivider(message) {
  if (!message || typeof message !== 'object') return false;
  if (message.role === 'session') return true;
  return Boolean(message.metadata && message.metadata.session_start);
}

export function updateContextUsage(messages = null, message = null) {
  let target = message;
  // 终态单消息入口（done/error/取消）：分割线画在这条消息下方 → 这轮的 usage 属于
  // 「已被划出上下文」的那一段，必须按「无用量」处理——模型调 reset_context 成功时，
  // 同一条 done 消息**同时**带满量 usage 与 session_start，正是全靠这一步分辨。
  let contextReset = Boolean(target) && isContextStartDivider(target);
  if (contextReset) target = null;
  if (!target && !contextReset && Array.isArray(messages)) {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const item = messages[index];
      // 从末尾向前扫到**最新一条**分割线就停：线以上（含被标记的那条）的 usage 一律不算，
      // 线以下没有 usage（分割线之后还没聊过）→ usage=null → 圆环归零、输入框解锁。
      // 撤销分割线后同一段扫描会重新看到线以上的 usage、按需回锁——语义自洽，无特殊分支。
      if (isContextStartDivider(item)) { contextReset = true; break; }
      if (item?.role === 'assistant' && item?.metadata?.usage) {
        target = item;
        break;
      }
    }
  }
  const usage = target?.metadata?.usage || null;
  const conversationId = String(state.conversationId || '');
  // 运行中：历史重渲染（会话/任务轮询）不得把实时圆环擦回「暂无数据」——本轮尚未
  // 落库，messages 里当然没有 usage；只有会话真正切换时才用新会话的历史值覆盖。
  // 例外：`contextReset` 是「上下文已被显式重置」的确证（分割线已落库），此刻即使
  // 还在收尾（chatBusy 尚未落下）也必须归零解锁，否则重置后满量锁会一直挂着。
  if (!usage && !contextReset && state.chatBusy && state.contextUsageConversationId === conversationId) return;
  setContextUsage(usage, conversationId);
}

// 上下文圆环/弹层的唯一写入点：流式期间每完成一次模型请求（usage 事件）就刷新，
// 不必等整轮结束；终态 done/error/取消 与历史渲染统一经 updateContextUsage 复用本入口。
export function setContextUsage(usage, conversationId = null) {
  state.contextUsage = usage || null;
  state.contextUsageConversationId = conversationId === null
    ? String(state.conversationId || '')
    : String(conversationId || '');
  renderContextUsage();
}

// 圆环与输入框下方「思考 自动」右侧的实时百分比共用同一套配色阈值。
const CONTEXT_TONE_WARNING = 70;
const CONTEXT_TONE_DANGER = 90;

// 「思考 自动」右侧的上下文百分比：与圆环同源，只由 renderContextUsage 写入。
function renderContextPercentLabel({ text = '上下文 --', title = '上下文用量：暂无数据', percent = 0 } = {}) {
  const label = $('#contextPercentLabel');
  if (!label) return;
  label.textContent = text;
  label.title = title;
  label.classList.toggle('warning', percent >= CONTEXT_TONE_WARNING && percent < CONTEXT_TONE_DANGER);
  label.classList.toggle('danger', percent >= CONTEXT_TONE_DANGER);
}

export function renderContextUsage() {
  const button = $('#contextUsageButton');
  const ring = $('#contextUsageRing');
  const summary = $('#contextUsageSummary');
  const turn = $('#lastTurnUsageSummary');
  if (!button || !ring || !summary || !turn) return;
  const usage = state.contextUsage;
  if (!usage) {
    ring.style.setProperty('--context-percent', '0');
    ring.classList.remove('warning', 'danger');
    button.title = '上下文用量：暂无数据';
    summary.textContent = '暂无模型用量数据';
    turn.textContent = '完成一次回复后显示本轮消耗';
    state.contextAtCeiling = false;
    state.contextPercent = 0;
    renderContextPercentLabel();
    maybeWarnContextUsage(0);
    updateContextComposerLock();
    return;
  }
  const input = Number(usage.input_tokens || 0);
  const output = Number(usage.output_tokens || 0);
  const total = Number(usage.total_tokens || input + output);
  const context = Number(usage.context_tokens || 0);
  const profiles = state.bootstrap?.model_profiles || state.bootstrap?.providers || [];
  const profile = profiles.find((item) => item.model_key === usage.model_key)
    || selectedProvider();
  const profileLimit = Number(profile?.context_window || 0);
  const trustedStoredLimit = usage.context_limit_source
    ? Number(usage.context_limit || 0)
    : 0;
  const limit = profileLimit || trustedStoredLimit;
  const percent = limit > 0 ? Math.min(100, Math.max(0, context / limit * 100)) : 0;
  state.contextPercent = percent;
  ring.style.setProperty('--context-percent', percent.toFixed(1));
  ring.classList.toggle('warning', percent >= CONTEXT_TONE_WARNING && percent < CONTEXT_TONE_DANGER);
  ring.classList.toggle('danger', percent >= CONTEXT_TONE_DANGER);
  const contextText = limit
    ? `${context.toLocaleString()} / ${limit.toLocaleString()}（${percent.toFixed(1)}%）`
    : context ? `${context.toLocaleString()} / 上限未知` : '上下文上限未知';
  button.title = `上下文用量：${contextText}`;
  summary.textContent = `上下文 ${contextText}`;
  turn.textContent = `最近一轮：输入 ${input.toLocaleString()} · 输出 ${output.toLocaleString()} · 总计 ${total.toLocaleString()} tokens`;
  renderContextPercentLabel({
    text: limit ? `上下文 ${percent.toFixed(1)}%` : '上下文 --',
    title: `上下文用量：${contextText}`,
    percent: limit ? percent : 0,
  });
  const atCeiling = limit > 0 && percent >= 100;
  if (atCeiling !== state.contextAtCeiling) {
    state.contextAtCeiling = atCeiling;
    if (atCeiling) toast('上下文已达到上限，请新建对话后继续。');
  }
  maybeWarnContextUsage(limit > 0 ? percent : 0);
  updateContextComposerLock(Boolean(state.chatBusy));
}

// 上下文提醒阈值（%）：0 = 关闭。取自「设置 → 运行设置」，随每次 usage 刷新重新读取，
// 因此改完设置无需重启即可生效。
export function contextWarningPercent() {
  const raw = Number(state.bootstrap?.settings?.context_warning_percent ?? 80);
  return Number.isFinite(raw) ? raw : 80;
}

// 距上次提醒再涨这么多个百分点就再提醒一次（不是每会话只提醒一次，也不是每次发送都提醒）。
const CONTEXT_WARNING_STEP = 5;

// 是否需要提醒：已达阈值，且距上次提醒已经又涨了 CONTEXT_WARNING_STEP 个百分点。
function contextWarningDue(percent, threshold) {
  if (!(threshold > 0) || !(percent > 0) || percent < threshold) return false;
  const warnedAt = Number(state.contextWarningAtPercent) || 0;
  return warnedAt <= 0 || percent >= warnedAt + CONTEXT_WARNING_STEP;
}

// 达到阈值时弹窗提醒：**以会话为单位**记录上次提醒的百分比——换会话重新武装，用量回落到
// 阈值以下也重新武装（例如新建对话）。percent<=0（无数据/上限未知）不提醒。
// **运行中才在这里弹**；空闲会话（例如只是切到旧会话）留给"点击发送"前的
// pendingContextWarning 判定，避免浏览旧会话就被打扰。
export function maybeWarnContextUsage(percent) {
  const threshold = contextWarningPercent();
  const conversationId = String(state.conversationId || '');
  if (state.contextWarningConversationId !== conversationId) {
    state.contextWarningConversationId = conversationId;
    state.contextWarningAtPercent = 0;
  }
  const value = Number(percent) || 0;
  if (!(threshold > 0) || !(value > 0) || value < threshold) {
    state.contextWarningAtPercent = 0;
    return;
  }
  if (!state.chatBusy) return;
  if (!contextWarningDue(value, threshold)) return;
  state.contextWarningAtPercent = value;
  showContextWarning(value, threshold, { mode: 'running' });
}

// 发送前判定（空闲会话）：已达阈值且距上次提醒又涨了 5% → 返回 {percent, threshold}
// 并记下本次提醒的百分比；调用方负责弹窗（弹窗里可选「继续发送」）。
export function pendingContextWarning() {
  const threshold = contextWarningPercent();
  const percent = Number(state.contextPercent) || 0;
  const conversationId = String(state.conversationId || '');
  if (state.contextWarningConversationId !== conversationId) return null;
  if (state.chatBusy) return null;
  if (!contextWarningDue(percent, threshold)) return null;
  state.contextWarningAtPercent = percent;
  return { percent, threshold };
}

// 弹窗待续动作（仅"发送前提醒"模式有）：关闭弹窗时必须清掉，避免误触发上一轮发送。
let contextWarningResume = null;

export function showContextWarning(percent, threshold, options = {}) {
  const detail = $('#contextWarningDetail');
  if (detail) {
    // 建议文案只在弹窗正文里出现一次，这里只报数（避免两处重复建议）。
    detail.textContent = `上下文用量已达 ${Number(percent).toFixed(1)}%（提醒阈值 ${threshold}%）`;
  }
  const continueButton = $('#contextWarningContinue');
  contextWarningResume = typeof options.onContinue === 'function' ? options.onContinue : null;
  if (continueButton) continueButton.hidden = !contextWarningResume;
  const dialog = $('#contextWarningDialog');
  if (dialog && !dialog.open) dialog.showModal();
}

// 「继续发送」：先取回待续动作再关弹窗（关闭事件会清空它）。
export function continueAfterContextWarning() {
  const resume = contextWarningResume;
  contextWarningResume = null;
  const dialog = $('#contextWarningDialog');
  if (dialog && dialog.open) dialog.close();
  if (typeof resume === 'function') resume();
}

export function resetContextWarningResume() {
  contextWarningResume = null;
}

export function updateContextComposerLock(busy = false) {
  const editing = Boolean(state.editingMessageId);
  const atCeiling = Boolean(state.contextAtCeiling) && !editing;
  const input = $('#messageInput');
  // 输入区移动到历史消息后继续接受输入；编辑重发会先截断历史，上下文占用随后重算。
  // 只有普通新消息在上下文已满时锁住输入，disabled/placeholder 仍由这里统一维护。
  if (input) {
    input.disabled = atCeiling;
    input.placeholder = editing
      ? '编辑消息，Enter 重新发送，Shift+Enter 换行，Esc 取消'
      : (atCeiling ? '上下文已满，请新建对话后继续' : (busy ? '回复进行中…（输入后 Enter 加入插话队列）' : '输入消息'));
  }
  // 发送按钮的可用性由 updateSendButtonState 单点维护（含"运行中即停止键"语义）。
  updateSendButtonState();
  // 收尾重算输入框高度（§九.128）：上面刚改了 placeholder，updateSendButtonState 又经
  // updateInterjectButtonState 切换了插话键显隐（= 输入区宽度 132px ↔ 88px）。这两件事都会
  // 改变折行点，不重算就会把上一次的高度留在屏幕上——用户手机截图里的「空输入框占半屏、
  // 本轮结束后也不回落」就是这么来的。放在最后，量的才是最终宽度下的真实内容高度。
  resizeTextarea();
}

// 发送按钮可用性（唯一写入点）：文字或附件至少有一个才可发送——纯附件轮次（只发文件/
// 图片、不写字）合法；上传未完成 / 上下文已满 / 无内容时 disabled（灰暗样式由
// styles.css 的 .send-button:disabled 承担）；回复进行中按钮变身"停止"，始终可点。
// 「编辑消息」态复用同一输入框与附件列表，按钮变成「重新发送」，同样等待上传完成。
export function updateSendButtonState() {
  const sendBtn = $('#sendButton');
  if (!sendBtn) return;
  const busy = Boolean(state.chatBusy);
  const cancelRequested = Boolean(state.cancelRequested);
  let disabled = false;
  let title = '发送';
  if (busy) {
    disabled = cancelRequested;
    title = cancelRequested ? '正在停止' : '停止当前任务';
  } else if (state.contextAtCeiling && !state.editingMessageId) {
    disabled = true;
    title = '上下文已满，请新建对话后继续';
  } else {
    const editing = Boolean(state.editingMessageId);
    const uploading = state.pendingFiles.find((file) => file.uploading);
    const hasText = Boolean(String($('#messageInput')?.value || '').trim());
    const hasAttachment = state.pendingFiles.some((file) => file.path);
    title = editing ? '重新发送（编辑中）' : '发送';
    if (uploading) {
      disabled = true;
      title = `请等待「${uploading.name}」上传完成`;
    } else if (!hasText && !hasAttachment) {
      disabled = true;
      title = editing ? '编辑消息或添加文件后重新发送' : '输入消息或添加文件后发送';
    }
  }
  sendBtn.disabled = disabled;
  sendBtn.title = title;
  sendBtn.setAttribute('aria-label', title);
  updateInterjectButtonState(busy);
}

// 「插话」按钮（回复进行中才出现）的可用性：有文字或附件才可点——空点没有意义。
// 关闭态用 hidden 而不是 disabled：运行结束后它整块消失，避免「发送」与「插话」
// 两个按钮并存让用户分不清哪个会真的发出去。与 sendButton 同属本模块的可用性口径，
// 由 updateSendButtonState 单点驱动（不要在各处另行改写这两个按钮）。
function updateInterjectButtonState(busy) {
  const button = $('#interjectButton');
  if (!button) return;
  button.hidden = !busy;
  if (!busy) {
    button.disabled = true;
    return;
  }
  const uploading = state.pendingFiles.find((file) => file.uploading);
  const hasText = Boolean(String($('#messageInput')?.value || '').trim());
  const hasAttachment = state.pendingFiles.some((file) => file.path);
  button.disabled = Boolean(uploading) || (!hasText && !hasAttachment);
  const title = uploading
    ? `请等待「${uploading.name}」上传完成`
    : '加入插话队列';
  button.title = title;
  button.setAttribute('aria-label', title);
}

// Legacy provider context_size: migrated to context_window and kept only for
// compatibility with older configuration readers.
// providerContextSize remains only as a hidden legacy selector marker;
// providerContextWindow is the active provider-scoped control.

export function positionContextUsagePopover() {
  const popover = $('#contextUsagePopover');
  const button = $('#contextUsageButton');
  if (!popover || !button || popover.hidden) return;
  const edge = 12;
  const gap = 9;
  const buttonRect = button.getBoundingClientRect();
  const popoverRect = popover.getBoundingClientRect();
  const rightAligned = buttonRect.right - popoverRect.width;
  const maxLeft = Math.max(edge, window.innerWidth - popoverRect.width - edge);
  const left = Math.min(Math.max(edge, rightAligned), maxLeft);
  let top = buttonRect.top - popoverRect.height - gap;
  if (top < edge) {
    top = Math.min(
      buttonRect.bottom + gap,
      Math.max(edge, window.innerHeight - popoverRect.height - edge),
    );
  }
  popover.style.left = `${Math.round(left)}px`;
  popover.style.top = `${Math.round(top)}px`;
}

export function toggleContextUsagePopover(event) {
  event.stopPropagation();
  const popover = $('#contextUsagePopover');
  const button = $('#contextUsageButton');
  const open = popover.hidden;
  if (open && popover.parentElement !== document.body) document.body.appendChild(popover);
  popover.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
  if (open) positionContextUsagePopover();
}

export function closeContextUsagePopover() {
  const popover = $('#contextUsagePopover');
  const button = $('#contextUsageButton');
  if (!popover || popover.hidden) return;
  popover.hidden = true;
  button?.setAttribute('aria-expanded', 'false');
}

export function skillMarkup(skills = []) {
  if (!Array.isArray(skills) || !skills.length) return '';
  const parts = [];
  const user = skills.filter((s) => s?.source !== 'auto');
  const auto = skills.filter((s) => s?.source === 'auto');
  if (user.length) parts.push(`已启用 Skill：${user.map((s) => escapeHtml(s?.name || s)).join('、')}`);
  if (auto.length) parts.push(`已自动匹配 Skill：${auto.map((s) => escapeHtml(s?.name || s)).join('、')}`);
  return parts.length ? `<div class="skill-usage">${parts.join('<br>')}</div>` : '';
}

export function sourcesMarkup(sources = []) {
  if (!Array.isArray(sources) || !sources.length) return '';
  const items = sources.map((source) => {
    const url = String(source?.url || '');
    if (!/^https?:\/\//i.test(url)) return '';
    const title = escapeHtml(source?.title || url);
    const snippet = escapeHtml(source?.snippet || '');
    const published = escapeHtml(source?.published_at || '');
    return `<li><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${title}</a>${published ? `<time>${published}</time>` : ''}${snippet ? `<p>${snippet}</p>` : ''}</li>`;
  }).filter(Boolean).join('');
  return items ? `<details class="message-sources"><summary>联网来源（${sources.length}）</summary><ol>${items}</ol></details>` : '';
}

// 消息末尾「本轮修改的文件」总结。桌面端文件名可点 → 打开右侧文件面板；
// 手机端（≤760px）由 CSS + openFilePanel 双重把关，仅展示、不可点。
export function fileChangesSummaryMarkup(files = []) {
  if (!Array.isArray(files) || !files.length) return '';
  let edited = 0;
  let created = 0;
  for (const f of files) {
    if (!f || !f.path) continue;
    if (f.op === 'edit') edited += 1;
    else created += 1;
  }
  if (!edited && !created) return '';
  const chips = files.map((f) => {
    if (!f || !f.path) return '';
    const raw = String(f.path);
    // 图片/视频/音频等多媒体产物走消息内媒体卡（mediaMarkup），不冒充"修改文件"chip。
    // 判定与后端同源（mediaKind ← /api/bootstrap.media_exts）：此前前端多算 .bmp/.svg，
    // 后端又不把它们当媒体，结果产物两边都不显示（静默消失）。
    if (mediaKind(raw, f.name) !== 'other') return '';
    const name = escapeHtml(f.name || raw.replace(/\\/g, '/').split('/').pop() || raw);
    const isEdit = f.op === 'edit';
    return `<button type="button" class="file-change-chip" data-file-op="${isEdit ? 'edit' : 'write'}" data-open-file="${escapeHtml(raw)}" title="${isEdit ? '编辑' : '新建'}：${escapeHtml(raw)}"><span class="file-change-op">${isEdit ? '改' : '新'}</span><span class="file-change-name">${name}</span></button>`;
  }).filter(Boolean).join('');
  if (!chips) return '';
  const opNote = [];
  if (created) opNote.push(`新建 ${created}`);
  if (edited) opNote.push(`编辑 ${edited}`);
  return `<div class="file-changes"><div class="file-changes-label">本轮修改文件${opNote.length ? `（${opNote.join(' · ')}）` : ''}</div><div class="file-changes-list">${chips}</div></div>`;
}

export function toolAvailabilityMarkup(tools = []) {
  if (!Array.isArray(tools) || !tools.length) return '';
  const items = tools.map((tool) => {
    const name = typeof tool === 'string' ? tool : tool?.name;
    if (!name) return '';
    const description = typeof tool === 'string' ? '' : String(tool?.description || '');
    return `<li><code>${escapeHtml(name)}</code>${description ? ` <span>${escapeHtml(description)}</span>` : ''}</li>`;
  }).filter(Boolean).join('');
  return items ? `<details class="tool-availability"><summary>Available tools (${tools.length})</summary><ul>${items}</ul></details>` : '';
}

