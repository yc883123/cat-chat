// ============================================================
// 10-upload.js —— 拆分自 public/app.js 第 4673-4709 行（阶段 5.1 按域拆分，跨文件引用零改动）
// 2026-09 上传系统优化：XHR multipart 流式上传（进度/取消/并发池/前置校验）。
// ============================================================

import { $, api, escapeHtml, state, toast } from "./01-core.js";
import { attachmentThumbUrl, fileUrl, mediaKind, updateSendButtonState } from "./03-media.js";

// 与服务端 UPLOAD_MAX_BYTES 一致的前置校验上限（超限直接拦截，不发起请求）。
export const UPLOAD_MAX_BYTES = 80 * 1024 * 1024;
// 并发上传上限：多文件同时传完更快，又不会打满连接。
const UPLOAD_CONCURRENCY = 3;

export function uploadFiles(files) {
  const valid = [];
  for (const file of files) {
    if (file.size > UPLOAD_MAX_BYTES) {
      toast(`「${file.name}」超过 80MB，已跳过`);
      continue;
    }
    valid.push(file);
  }
  if (!valid.length) return;
  // 并发池：每次完成一个立即补位，保持最多 UPLOAD_CONCURRENCY 个在传。
  let index = 0;
  const runNext = () => {
    if (index >= valid.length) return;
    const file = valid[index++];
    uploadOne(file).finally(runNext);
  };
  const startCount = Math.min(UPLOAD_CONCURRENCY, valid.length);
  for (let i = 0; i < startCount; i++) runNext();
}

function uploadOne(file) {
  const chip = { name: file.name, uploading: true, progress: 0 };
  state.pendingFiles.push(chip);
  renderPendingFiles();
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    // 取消句柄：chip 移除时由 renderPendingFiles 的移除按钮调用。
    chip.cancel = () => {
      try { xhr.abort(); } catch (_) { /* 已结束 */ }
    };
    xhr.open('POST', '/api/uploads');
    if (state.token) xhr.setRequestHeader('Authorization', `Bearer ${state.token}`);
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      chip.progress = Math.round((event.loaded / event.total) * 100);
      renderPendingFiles();
    };
    xhr.onload = () => {
      chip.cancel = null;
      let payload = {};
      try { payload = JSON.parse(xhr.responseText || '{}'); } catch (_) { /* 非 JSON 错误体 */ }
      if (xhr.status >= 200 && xhr.status < 300 && payload.path) {
        Object.assign(chip, payload, { uploading: false, progress: 100 });
      } else {
        state.pendingFiles = state.pendingFiles.filter((item) => item !== chip);
        toast(`上传失败：${payload.error || `HTTP ${xhr.status}`}`);
      }
      renderPendingFiles();
      resolve();
    };
    xhr.onerror = () => {
      chip.cancel = null;
      state.pendingFiles = state.pendingFiles.filter((item) => item !== chip);
      toast(`上传失败：网络错误`);
      renderPendingFiles();
      resolve();
    };
    xhr.onabort = () => {
      chip.cancel = null;
      resolve();
    };
    const form = new FormData();
    form.append('file', file, file.name);
    xhr.send(form);
  });
}

export function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// 非图片附件的占位图标（与图片缩略图同尺寸，保证整列左缘对齐）。
const FILE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"></path><path d="M14 3v5h5"></path></svg>';
// 文件夹占位图标（拖入文件夹时用，见 addFolderChip）。
const FOLDER_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"></path></svg>';

// ---- 待发送的文件夹（拖进来只显示一个占位，不铺缩略图）---------------------------
// 目录拖进来时**不逐文件上传**：气泡里只显示一个占位 chip，消息里带的是"路径 + 文件索引"，
// 让模型知道这个目录里有什么（用户不必逐个复制文件路径）。索引由后端扫（见
// `naiba/core/conv_files.py::folder_index`，深度/条数/超时三重闸门）。
// 浏览器端拿不到拖入文件夹的绝对路径（Chromium 安全模型），所以这条链路只在桌面壳里成立。

export function isFolderChip(item) {
  return String(item?.kind || '') === 'folder';
}

/** 待发送的普通附件（排除文件夹 chip：目录路径不能当附件发给模型）。 */
export function attachmentChips() {
  return state.pendingFiles.filter((item) => !isFolderChip(item));
}

export function folderChips() {
  return state.pendingFiles.filter(isFolderChip);
}

/** 把消息 metadata 里的文件夹索引还原成待发送 chip（编辑/重发/重新生成/分支时用）。 */
export function folderChipsFromIndexes(indexes = []) {
  return (Array.isArray(indexes) ? indexes : [])
    .filter((item) => item && String(item.path || '').trim())
    .map((item) => ({
      kind: 'folder',
      name: String(item.name || '').trim() || String(item.path).split(/[\\/]/).pop() || '文件夹',
      path: String(item.path),
      total: Number(item.total || 0),
      image_count: Number(item.image_count || 0),
      dir_count: Number(item.dir_count || 0),
      truncated: Boolean(item.truncated),
      entries: Array.isArray(item.entries) ? item.entries : [],
      existingFolder: true,   // 来自历史消息：不进"删除未引用缓存"那条链路
    }));
}

/** 把索引对象裁成消息 metadata 的形状（乐观气泡要用，与后端落库字段同名）。 */
export function folderIndexMetadata(chip) {
  return {
    name: chip.name,
    path: chip.path,
    total: chip.total,
    image_count: chip.image_count,
    dir_count: chip.dir_count,
    truncated: Boolean(chip.truncated),
    entries: chip.entries || [],
  };
}

function fetchFolderIndex(path, { allowOutside = false } = {}) {
  const params = new URLSearchParams({ path: String(path || '') });
  if (state.conversationId) params.set('conversation_id', state.conversationId);
  if (allowOutside) params.set('allow_outside', '1');
  return api(`/api/files/folder-index?${params.toString()}`);
}

/** 工作区外文件夹的确认框（口径二：确认一次，按会话记住）。 */
function askFolderAccess(path) {
  const dialog = $('#folderAccessDialog');
  // 弹层缺失（浏览器缓存了旧 index.html）时按"不授权"处理：宁可让用户再拖一次，
  // 也不能在读不到提示的情况下默默读取工作区外的目录。
  if (!dialog) return Promise.resolve(false);
  const target = $('#folderAccessPath');
  if (target) target.textContent = String(path || '');
  return new Promise((resolve) => {
    let settled = false;
    const buttons = {
      allow: $('#folderAccessAllow'),
      deny: $('#folderAccessDeny'),
      cancel: $('#folderAccessCancel'),
    };
    const cleanup = () => {
      buttons.allow?.removeEventListener('click', onAllow);
      buttons.deny?.removeEventListener('click', onDeny);
      buttons.cancel?.removeEventListener('click', onCancel);
      dialog.removeEventListener('close', onCancel);
    };
    const done = (value) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (dialog.open && typeof dialog.close === 'function') dialog.close();
      resolve(value);
    };
    function onAllow() { done(true); }
    function onDeny() { done(false); }
    function onCancel() { done(false); }
    buttons.allow?.addEventListener('click', onAllow);
    buttons.deny?.addEventListener('click', onDeny);
    buttons.cancel?.addEventListener('click', onCancel);
    dialog.addEventListener('close', onCancel);
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  });
}

/**
 * 把一个文件夹加进待发送列表（只建一个占位 chip，不展开、不传内容）。
 * 工作区外的路径会先弹一次确认（403 + needs_confirm）。
 */
export async function addFolderChip(path, { allowOutside = false, quiet = false } = {}) {
  const raw = String(path || '').trim();
  if (!raw) return null;
  if (state.pendingFiles.some((item) => isFolderChip(item) && String(item.path || '') === raw)) {
    toast('这个文件夹已经在待发送列表里了');
    return null;
  }
  let index;
  try {
    index = await fetchFolderIndex(raw, { allowOutside });
  } catch (error) {
    if (error?.status === 403 && error?.payload?.needs_confirm) {
      const allowed = await askFolderAccess(error.payload.path || raw);
      if (!allowed) return null;
      return addFolderChip(raw, { allowOutside: true, quiet });
    }
    toast(`无法读取文件夹：${error.message}`);
    return null;
  }
  const chip = {
    kind: 'folder',
    name: String(index?.name || '').trim() || raw.split(/[\\/]/).pop() || '文件夹',
    path: String(index?.path || raw),
    total: Number(index?.total || 0),
    image_count: Number(index?.image_count || 0),
    dir_count: Number(index?.dir_count || 0),
    truncated: Boolean(index?.truncated),
    entries: Array.isArray(index?.entries) ? index.entries : [],
  };
  state.pendingFiles.push(chip);
  renderPendingFiles();
  if (!quiet) {
    toast(`已加入文件夹「${chip.name}」：${chip.total} 项（含 ${chip.image_count} 图）`);
  }
  return chip;
}

// 缩略图加载失败兜底（两级，与消息气泡的 .thumbnail → 主图 同策略）：
//   ① 缩略图不在但主图还在 → 换成主图（`_thumb.webp` 可能从未生成或已被单独清掉）；
//   ② 主图也没了（被缓存清理/移动/删除）→ 降级为文件占位图标并摘掉失效的
//      data-large-url——否则留在那里的破图既误导用户，点击还会打开一个必然 404 的灯箱。
// 用 document 捕获阶段监听：renderPendingFiles 每次整体重绘 innerHTML，挂在容器上会随重绘丢失。
document.addEventListener('error', (event) => {
  const img = event.target;
  if (!(img && img.classList?.contains('pending-thumb'))) return;
  const large = String(img.getAttribute('data-large-url') || '');
  if (large && img.getAttribute('src') !== large) {
    img.dataset.fallback = '1';
    img.src = large;
    return;
  }
  const icon = document.createElement('span');
  icon.className = 'pending-thumb pending-thumb-file';
  icon.setAttribute('aria-hidden', 'true');
  icon.title = '文件已不存在（可能已被缓存清理）';
  icon.innerHTML = FILE_ICON;
  img.replaceWith(icon);
}, true);

// 发送前附件落地校验（服务端 /api/uploads/check 同一口径）：返回已丢失的本地路径。
// 起因：附件可能已被缓存清理（被引用缓存超阈值时的自动清理曾误删刚落盘的待发附件），
// 提交前先问一次，免得把幽灵路径喂给模型（模型只会回"未找到图片文件"，用户看不出原因）。
// 校验本身失败（网络/接口异常）不阻断发送——服务端提交时还会再拦一次。
export async function missingAttachmentPaths(paths = []) {
  const local = [...new Set(
    (paths || [])
      .map((item) => String(item || '').trim())
      .filter((item) => item && !/^https?:\/\//i.test(item)),
  )];
  if (!local.length) return [];
  try {
    const result = await api('/api/uploads/check', { method: 'POST', body: { paths: local } });
    return Array.isArray(result?.missing) ? result.missing : [];
  } catch (_) {
    return [];
  }
}

// 待发送附件：输入框上方的**竖直列表**（固定高度、可滚动、文件名截断、图片带预览）。
// 此前是横向 chip 条，文件名一长就一屏显示不全、还要横向拖滚动条。
export function renderPendingFiles() {
  const container = $('#pendingFiles');
  if (!container) return;
  container.innerHTML = state.pendingFiles.map((file, index) => {
    if (isFolderChip(file)) {
      // 文件夹占位：图标 + 名字 + 统计 + 移除。刻意**不展开**——一个素材目录能拖出几百个
      // 缩略图，输入区会被刷屏（用户要的是"让 AI 知道这目录里有什么"，不是"在这里翻图"）。
      const stats = `${file.total ?? 0} 项 · 含 ${file.image_count ?? 0} 图`
        + (file.dir_count ? ` · ${file.dir_count} 个子目录` : '')
        + (file.truncated ? ' · 已截断' : '');
      return `<div class="pending-item is-folder">
      <span class="pending-thumb pending-thumb-folder" aria-hidden="true">${FOLDER_ICON}</span>
      <span class="pending-name" title="${escapeHtml(file.path || file.name)}">${escapeHtml(file.name)}</span>
      <span class="pending-status">${escapeHtml(stats)}</span>
      <button type="button" class="pending-remove" data-remove-file="${index}" title="移除" aria-label="移除 ${escapeHtml(file.name)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
    </div>`;
    }
    const isImage = Boolean(file.path || file.thumb_path) && mediaKind(file.path, file.name) === 'image';
    // 上传中/无 path 时不渲染缩略图（旧逻辑会请求空路径 /api/file?path= → 404 破图）。
    const thumbUrl = file.path ? attachmentThumbUrl(file) : '';
    const preview = (isImage && thumbUrl)
      ? `<img class="pending-thumb" src="${escapeHtml(thumbUrl)}" alt="" draggable="false" data-large-url="${escapeHtml(fileUrl(file.path))}">`
      : `<span class="pending-thumb pending-thumb-file" aria-hidden="true">${FILE_ICON}</span>`;
    const status = file.uploading
      ? `<span class="pending-status">${file.progress > 0 ? `${file.progress}%` : '上传中'}</span>`
      : '';
    return `<div class="pending-item${file.uploading ? ' is-uploading' : ''}">
      ${preview}
      <span class="pending-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
      ${status}
      <button type="button" class="pending-remove" data-remove-file="${index}" title="移除" aria-label="移除 ${escapeHtml(file.name)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button>
    </div>`;
  }).join('');
  container.hidden = state.pendingFiles.length === 0;
  // 待发送附件增减直接决定"能否发送"（纯附件轮次合法）：单点刷新发送按钮状态。
  updateSendButtonState();
}
