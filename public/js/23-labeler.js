// ============================================================
// 23-labeler.js —— 工作台「打标器」功能页（docs/plans/2026-10-23-打标器接入工作台计划.md）
// 批量打标（模型来自设置里的 provider，云端与本地后端都算）：
// 文件夹拖入 → 逐批上传到受管 uploads → 提交批次 → 逐图进度 →
// 结果三档（按图 / 按标签 / 句子库）→ 导出 JSON / CSV / .txt。
// 与聊天域零耦合；只调 /api/labeler/* 与既有 /api/uploads。
// ============================================================

import { $, $$, api, state, toast, triggerDownload } from "./01-core.js";
import { fileUrl } from "./03-media.js";
import { currentStudioPage, onStudioPageChange } from "./22-studio.js";

// ---- 与服务端一致的批量上限（前端只做提前提示，服务端仍逐项校验） ----
const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".webp", ".gif"]);
const MAX_BATCH_IMAGES = 500;
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const MAX_BATCH_BYTES = 1024 * 1024 * 1024;
// 上传并发：与聊天附件一致（3），既能跑满带宽又不会打爆连接。
const UPLOAD_CONCURRENCY = 3;
const POLL_MS = 1200;
const SEARCH_DEBOUNCE_MS = 300;

const STATUS_LABELS = {
  queued: "排队中",
  running: "打标中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const ITEM_STATE_LABELS = {
  queued: "待处理",
  running: "处理中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  uncertain: "结果不确定",
};

const labeler = {
  initialized: false,
  presets: [],
  presetId: "",
  providerKey: "",
  modelName: "",
  providerModelsCache: new Map(),
  // 待上传/已上传图片：status = pending | uploading | uploaded | failed
  selected: [],
  uploadToken: 0,
  uploading: false,
  // 当前批次
  taskId: "",
  task: null,
  items: [],
  annotations: [],
  // 按标签视图命中集：与按图共用 annotations 会互相覆盖，单独存一份。
  tagMatches: [],
  tags: [],
  sentences: [],
  resultTab: "images",
  search: "",
  activeTag: "",
  batches: [],
  pollTimer: null,
  pollBusy: false,
  detailToken: 0,
  searchTimer: null,
  pane: "config",
};

// ============================================================
// 小工具
// ============================================================

function setHint(text, isError = false) {
  const node = $("#labelerTaskHint");
  if (!node) return;
  node.textContent = String(text || "");
  node.classList.toggle("error", Boolean(isError));
}

function setPickStatus(text) {
  const node = $("#labelerPickStatus");
  if (node) node.textContent = String(text || "");
}

function extOf(name) {
  const index = String(name || "").lastIndexOf(".");
  return index >= 0 ? String(name).slice(index).toLowerCase() : "";
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function selectedCounts() {
  const counts = { pending: 0, uploading: 0, uploaded: 0, failed: 0 };
  for (const row of labeler.selected) counts[row.status] = (counts[row.status] || 0) + 1;
  return counts;
}

let selectedUiTimer = null;

function scheduleSelectedUi() {
  if (selectedUiTimer) return;
  selectedUiTimer = setTimeout(() => {
    selectedUiTimer = null;
    updateSelectedUi();
  }, 150);
}

function updateSelectedUi() {
  const wrap = $("#labelerSelected");
  const strip = $("#labelerSelectedStrip");
  const summary = $("#labelerSelectedSummary");
  const retryBtn = $("#labelerRetryUpload");
  if (!wrap || !strip || !summary) return;
  const counts = selectedCounts();
  wrap.hidden = labeler.selected.length === 0;
  if (retryBtn) retryBtn.hidden = counts.failed === 0;
  const parts = [`已选 ${labeler.selected.length} 张`];
  if (counts.uploaded) parts.push(`已上传 ${counts.uploaded}`);
  if (counts.uploading) parts.push(`上传中 ${counts.uploading}`);
  if (counts.failed) parts.push(`失败 ${counts.failed}`);
  summary.textContent = parts.join(" · ");
  strip.replaceChildren(...labeler.selected.slice(0, 60).map((row) => {
    const item = document.createElement("div");
    item.className = "labeler-thumb";
    const img = document.createElement("img");
    img.alt = row.name;
    img.loading = "lazy";
    // 每个文件只建一次 object URL（500 张图时反复 create 会泄漏内存）。
    if (!row.objectUrl) row.objectUrl = URL.createObjectURL(row.file);
    img.src = row.objectUrl;
    const label = document.createElement("span");
    const state = row.status === "uploaded" ? "" : `（${
      row.status === "failed" ? "失败" : row.status === "uploading" ? `${row.progress || 0}%` : "待上传"
    }）`;
    label.textContent = row.name + state;
    label.title = row.relative_path || row.name;
    item.append(img, label);
    return item;
  }));
  const startBtn = $("#labelerStart");
  if (startBtn) {
    startBtn.disabled = labeler.uploading || counts.uploaded === 0;
    startBtn.textContent = labeler.uploading ? "正在上传…" : "开始打标";
  }
  updateCostNote();
}

function selectedProviderKind() {
  const key = String(labeler.providerKey || $("#labelerProviderSelect")?.value || "");
  const profiles = state.bootstrap?.model_profiles || state.bootstrap?.providers || [];
  return String(profiles.find((profile) => profile.model_key === key)?.kind || "");
}

function updateCostNote() {
  const node = $("#labelerCostNote");
  if (!node) return;
  const counts = selectedCounts();
  const uploaded = counts.uploaded;
  const local = selectedProviderKind() === "local";
  const localNote = local
    ? "本地模型与聊天共用同一份执行资源：打标按一张一张来，聊天仍可用，只是会等当前这张处理完。"
    : "打标在后台线程执行，不占用聊天页面；聊天与其它功能可以同时使用。";
  if (!uploaded) {
    node.textContent = `拖入文件夹后，先上传图片；开始前会显示张数与最多请求数。${localNote}`;
    return;
  }
  node.textContent = `本批 ${uploaded} 张：将发起最多 ${uploaded} 次请求（失败重试可能增加）。`
    + "费用由供应商按实际用量计，结束后汇总显示；单价无法事先预估。"
    + localNote;
}

// ============================================================
// 初始化
// ============================================================

async function ensurePresets() {
  if (labeler.presets.length) return;
  try {
    const payload = await api("/api/labeler/presets");
    labeler.presets = payload.presets || [];
    const select = $("#labelerPreset");
    if (select) {
      select.replaceChildren(...labeler.presets.map((preset) => {
        const option = document.createElement("option");
        option.value = preset.id;
        option.textContent = preset.label;
        return option;
      }));
      if (!labeler.presetId && labeler.presets.length) labeler.presetId = labeler.presets[0].id;
      if (labeler.presetId) select.value = labeler.presetId;
    }
    renderPresetDescription();
  } catch (error) {
    setHint(`打标预设读取失败：${error.message}`, true);
  }
}

function renderPresetDescription() {
  const node = $("#labelerPresetDesc");
  if (!node) return;
  const preset = labeler.presets.find((row) => row.id === labeler.presetId);
  node.textContent = preset ? preset.description : "";
}

async function ensureModels() {
  const providerSelect = $("#labelerProviderSelect");
  if (!providerSelect) return;
  const profiles = state.bootstrap?.model_profiles || state.bootstrap?.providers || [];
  if (providerSelect.options.length === 0) {
    providerSelect.replaceChildren(...profiles.map((profile) => {
      const option = document.createElement("option");
      option.value = profile.model_key;
      option.textContent = profile.name || profile.model_key;
      return option;
    }));
    const preferred = state.bootstrap?.default_model_key || profiles[0]?.model_key || "";
    providerSelect.value = labeler.providerKey && profiles.some((p) => p.model_key === labeler.providerKey)
      ? labeler.providerKey
      : preferred;
  }
  labeler.providerKey = providerSelect.value;
  await loadModelOptions();
}

async function loadModelOptions() {
  const modelSelect = $("#labelerModelSelect");
  if (!modelSelect) return;
  const providerKey = String(labeler.providerKey || "");
  if (!providerKey) {
    modelSelect.replaceChildren();
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "请先在设置中添加模型";
    modelSelect.append(option);
    return;
  }
  if (labeler.modelName && [...modelSelect.options].some((o) => o.value === labeler.modelName)) {
    modelSelect.value = labeler.modelName;
    return;
  }
  let models = labeler.providerModelsCache.get(providerKey);
  if (!models) {
    try {
      const payload = await api("/api/providers/models", { method: "POST", body: { model_key: providerKey } });
      models = payload.models || [];
      labeler.providerModelsCache.set(providerKey, models);
    } catch (error) {
      models = [];
      labeler.providerModelsCache.set(providerKey, models);
      setHint(`模型目录读取失败：${error.message}`, true);
    }
  }
  modelSelect.replaceChildren();
  if (!models.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "请先在设置中检查模型";
    modelSelect.append(option);
    return;
  }
  modelSelect.append(...models.map((model) => {
    const option = document.createElement("option");
    option.value = String(model.id || model.name || "");
    option.textContent = String(model.name || model.id || "");
    return option;
  }));
  const preferred = labeler.modelName && models.some((m) => String(m.id) === labeler.modelName)
    ? labeler.modelName
    : String(models[0]?.id || "");
  modelSelect.value = preferred;
  labeler.modelName = preferred;
}

let labelerActivation = null;

async function activateLabeler({ refresh = true } = {}) {
  // 切页时 setStudioPage 与 refreshStudio 会各触发一次：合并成一次，别并发拉两遍批次。
  if (labelerActivation) return labelerActivation;
  labelerActivation = (async () => {
    if (!labeler.initialized) {
      labeler.initialized = true;
      setLabelerPane(labeler.pane);
      await ensurePresets();
      await ensureModels();
    }
    if (refresh) {
      await refreshBatches();
      // 离开期间任务可能已结束：按服务端详情校准，而不是信旧快照。
      await reconcileTask();
    }
  })().finally(() => { labelerActivation = null; });
  return labelerActivation;
}

// ============================================================
// 手机三档
// ============================================================

function setLabelerPane(pane) {
  const next = ["queue", "config", "results"].includes(pane) ? pane : "config";
  labeler.pane = next;
  const page = $("#studioPageLabeler");
  if (page) page.dataset.labelerPane = next;
  $$("#labelerPanes .labeler-pane-btn").forEach((btn) => {
    const active = btn.dataset.labelerPane === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

// ============================================================
// 文件夹拖放与上传
// ============================================================

function entryToFile(entry) {
  return new Promise((resolve) => {
    entry.file((file) => resolve({ file, fullPath: entry.fullPath || file.name }), () => resolve(null));
  });
}

function readDirectoryEntries(reader) {
  return new Promise((resolve, reject) => {
    reader.readEntries((rows) => resolve(rows), reject);
  });
}

async function walkEntry(entry, out, depth = 0) {
  if (!entry || depth > 24) return;
  if (entry.isFile) {
    const row = await entryToFile(entry);
    if (row) out.push(row);
    return;
  }
  if (!entry.isDirectory) return;
  const reader = entry.createReader();
  // readEntries 一次只给一批（Chrome/WebView2 通常 100 条），必须循环读到空。
  for (;;) {
    const rows = await readDirectoryEntries(reader);
    if (!rows.length) break;
    for (const child of rows) await walkEntry(child, out, depth + 1);
  }
}

async function collectDropFiles(event) {
  const data = event.dataTransfer;
  if (!data) return [];
  const out = [];
  const items = data.items ? [...data.items] : [];
  const entries = [];
  for (const item of items) {
    if (item.kind !== "file") continue;
    const entry = typeof item.webkitGetAsEntry === "function" ? item.webkitGetAsEntry() : null;
    if (entry) entries.push(entry);
  }
  if (entries.length) {
    for (const entry of entries) await walkEntry(entry, out);
    return out;
  }
  // 备用：直接拖入若干图片文件（不含目录）。
  return [...(data.files || [])].map((file) => ({ file, fullPath: file.webkitRelativePath || file.name }));
}

function normalizeRelativePath(raw, fallback) {
  const text = String(raw || "").replace(/\\/g, "/").replace(/^\/+/, "");
  return text || String(fallback || "");
}

async function acceptFiles(picked) {
  if (labeler.uploading) {
    setPickStatus("正在上传，先取消或等本批上传完成");
    return;
  }
  const images = [];
  let skipped = 0;
  let totalBytes = 0;
  for (const row of picked) {
    if (!IMAGE_EXTS.has(extOf(row.file.name))) {
      skipped += 1;
      continue;
    }
    images.push(row);
  }
  if (!images.length) {
    setPickStatus(skipped ? `没有可用的图片（跳过 ${skipped} 个非图片文件）` : "没有可用的图片");
    return;
  }
  if (images.length > MAX_BATCH_IMAGES) {
    setPickStatus(`本批 ${images.length} 张，超过每批 ${MAX_BATCH_IMAGES} 张上限：请拆成多个文件夹后再试`);
    return;
  }
  for (const row of images) {
    if (row.file.size > MAX_IMAGE_BYTES) {
      setPickStatus(`「${row.file.name}」超过 ${formatBytes(MAX_IMAGE_BYTES)} 上限：请先压缩后再试`);
      return;
    }
    totalBytes += row.file.size;
  }
  if (totalBytes > MAX_BATCH_BYTES) {
    setPickStatus(`本批合计 ${formatBytes(totalBytes)}，超过 ${formatBytes(MAX_BATCH_BYTES)} 上限：请拆成多批`);
    return;
  }
  // 替换旧选择：一次拖入 = 一个批次；重复拖入不会把两批混在一起。
  abortUploads();
  labeler.selected = images.map((row, index) => ({
    id: `${Date.now()}-${index}`,
    file: row.file,
    name: row.file.name,
    relative_path: normalizeRelativePath(row.fullPath, row.file.name),
    size: row.file.size,
    status: "pending",
    progress: 0,
    path: "",
    thumb_path: "",
    error: "",
    xhr: null,
  }));
  const groups = new Set(labeler.selected.map((row) => String(row.relative_path).split("/")[0]).filter(Boolean));
  setPickStatus(
    `已选 ${labeler.selected.length} 张`
    + (groups.size > 1 ? `，来自 ${groups.size} 个顶层文件夹` : "")
    + (skipped ? `；跳过 ${skipped} 个非图片文件` : "")
  );
  updateSelectedUi();
  await uploadAll();
}

function abortUploads() {
  labeler.uploadToken += 1;
  for (const row of labeler.selected) {
    if (row.xhr) {
      try { row.xhr.abort(); } catch (_) { /* 已结束 */ }
      row.xhr = null;
    }
    if (row.status === "uploading") row.status = "pending";
  }
  labeler.uploading = false;
}

function uploadOne(row, token) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    row.xhr = xhr;
    row.status = "uploading";
    row.progress = 0;
    row.error = "";
    xhr.open("POST", "/api/uploads");
    if (state.token) xhr.setRequestHeader("Authorization", `Bearer ${state.token}`);
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      row.progress = Math.round((event.loaded / event.total) * 100);
      // 500 张的批次里进度事件非常密集：重绘缩略条要节流，否则界面会卡。
      scheduleSelectedUi();
    };
    xhr.onload = () => {
      row.xhr = null;
      if (token !== labeler.uploadToken) return resolve();
      let payload = {};
      try { payload = JSON.parse(xhr.responseText || "{}"); } catch (_) { /* 非 JSON */ }
      if (xhr.status >= 200 && xhr.status < 300 && payload.path) {
        row.status = "uploaded";
        row.path = String(payload.path);
        row.thumb_path = String(payload.thumb_path || "");
        row.progress = 100;
        // 服务端按内容去重，可能返回同内容已存在的文件名：以服务端为准。
        if (payload.name) row.name = String(payload.name);
      } else {
        row.status = "failed";
        row.error = String(payload.error || `HTTP ${xhr.status}`);
      }
      updateSelectedUi();
      resolve();
    };
    xhr.onerror = () => {
      row.xhr = null;
      if (token !== labeler.uploadToken) return resolve();
      row.status = "failed";
      row.error = "网络错误";
      updateSelectedUi();
      resolve();
    };
    xhr.onabort = () => {
      row.xhr = null;
      resolve();
    };
    const form = new FormData();
    form.append("file", row.file, row.file.name);
    xhr.send(form);
  });
}

async function uploadAll({ retryFailed = false } = {}) {
  if (labeler.uploading) return;
  const wanted = labeler.selected.filter((row) =>
    retryFailed ? row.status === "failed" : row.status === "pending");
  if (!wanted.length) return;
  const token = ++labeler.uploadToken;
  labeler.uploading = true;
  updateSelectedUi();
  setPickStatus(`正在上传 ${wanted.length} 张…`);
  let index = 0;
  const runNext = async () => {
    while (index < wanted.length) {
      if (token !== labeler.uploadToken) return;
      const row = wanted[index++];
      await uploadOne(row, token);
    }
  };
  await Promise.all(Array.from({ length: Math.min(UPLOAD_CONCURRENCY, wanted.length) }, runNext));
  if (token !== labeler.uploadToken) return;
  labeler.uploading = false;
  updateSelectedUi();
  const counts = selectedCounts();
  if (counts.failed) {
    setPickStatus(`上传完成：成功 ${counts.uploaded} 张，失败 ${counts.failed} 张（可点「重试失败」）`);
  } else {
    setPickStatus(`上传完成：${counts.uploaded} 张，可以开始打标`);
  }
}

// ============================================================
// 提交批次 / 轮询
// ============================================================

function collectConfig() {
  const preset = $("#labelerPreset")?.value || labeler.presetId || "";
  const providerSelect = $("#labelerProviderSelect");
  const modelSelect = $("#labelerModelSelect");
  return {
    engine: "provider",
    output_mode: $("#labelerOutputMode")?.value || "keywords",
    preset,
    model_key: String(providerSelect?.value || ""),
    model_name: String(modelSelect?.value || ""),
    confidence_threshold: Number($("#labelerConfidence")?.value || 0.35),
    max_tags: Number($("#labelerMaxTags")?.value || 24),
    custom_prompt: String($("#labelerCustomPrompt")?.value || "").trim(),
    concurrency: Number($("#labelerConcurrency")?.value || 1),
    retry_limit: Number($("#labelerRetryLimit")?.value || 2),
    timeout_seconds: Number($("#labelerTimeout")?.value || 90),
    prompt_version: 1,
  };
}

async function startTask() {
  const uploaded = labeler.selected.filter((row) => row.status === "uploaded" && row.path);
  if (!uploaded.length) {
    setHint("请先拖入文件夹并等待图片上传完成", true);
    return;
  }
  const config = collectConfig();
  if (!config.model_key || !config.model_name) {
    setHint("请选择模型配置与模型", true);
    return;
  }
  const failed = labeler.selected.filter((row) => row.status !== "uploaded").length;
  if (failed) {
    const proceed = window.confirm(`还有 ${failed} 张没有上传成功，本次只打标已上传的 ${uploaded.length} 张。继续吗？`);
    if (!proceed) return;
  }
  setHint("正在提交批次…");
  const startBtn = $("#labelerStart");
  if (startBtn) startBtn.disabled = true;
  try {
    const payload = await api("/api/labeler/tasks", {
      method: "POST",
      body: {
        image_refs: uploaded.map((row) => ({
          path: row.path,
          thumb_path: row.thumb_path || "",
          name: row.name,
          relative_path: row.relative_path,
        })),
        config,
      },
    });
    const task = payload.task || {};
    labeler.taskId = String(task.id || "");
    labeler.task = task;
    labeler.items = task.items || [];
    labeler.annotations = [];
    labeler.tagMatches = [];
    labeler.tags = [];
    labeler.sentences = [];
    setResultTab("images");
    renderTask(task);
    await refreshBatches();
    startPolling();
    if (window.matchMedia("(max-width: 760px)").matches) setLabelerPane("results");
    setHint("");
  } catch (error) {
    setHint(`提交失败：${error.message}`, true);
  } finally {
    if (startBtn) startBtn.disabled = false;
    updateSelectedUi();
  }
}

function startPolling() {
  stopPolling();
  if (currentStudioPage() !== "labeler") return;
  labeler.pollTimer = setInterval(() => { void pollTask(); }, POLL_MS);
}

function stopPolling() {
  if (labeler.pollTimer) {
    clearInterval(labeler.pollTimer);
    labeler.pollTimer = null;
  }
}

async function pollTask() {
  const taskId = labeler.taskId;
  if (!taskId || labeler.pollBusy) return;
  labeler.pollBusy = true;
  try {
    const { task } = await api(`/api/labeler/tasks/${encodeURIComponent(taskId)}`);
    if (labeler.taskId !== taskId) return;
    labeler.task = task;
    labeler.items = task.items || [];
    renderTask(task);
    if (!["queued", "running"].includes(String(task.status || ""))) {
      stopPolling();
      await loadTaskResults(taskId);
      await refreshBatches();
    }
  } catch (error) {
    if (error.status === 404) {
      stopPolling();
      labeler.taskId = "";
      setHint("任务记录已不存在，已回到空状态", true);
    }
  } finally {
    labeler.pollBusy = false;
  }
}

// 重进打标页 / 页面刷新后按服务端校准当前批次。
async function reconcileTask() {
  const taskId = labeler.taskId;
  if (!taskId) return;
  const token = ++labeler.detailToken;
  try {
    const { task } = await api(`/api/labeler/tasks/${encodeURIComponent(taskId)}`);
    if (token !== labeler.detailToken || labeler.taskId !== taskId) return;
    labeler.task = task;
    labeler.items = task.items || [];
    renderTask(task);
    if (["queued", "running"].includes(String(task.status || ""))) startPolling();
    else {
      stopPolling();
      await loadTaskResults(taskId);
    }
  } catch (error) {
    if (token !== labeler.detailToken) return;
    stopPolling();
    if (error.status === 404) {
      labeler.taskId = "";
      return;
    }
    setHint(`任务状态读取失败：${error.message}`, true);
  }
}

async function cancelTask() {
  const taskId = labeler.taskId;
  if (!taskId) return;
  const cancelBtn = $("#labelerCancel");
  if (cancelBtn) cancelBtn.disabled = true;
  try {
    await api(`/api/labeler/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST", body: {} });
    setHint("已请求取消：正在处理的这张可能仍会被供应商计费。");
    await pollTask();
  } catch (error) {
    setHint(`取消失败：${error.message}`, true);
  } finally {
    if (cancelBtn) cancelBtn.disabled = false;
  }
}

async function continueTask() {
  const taskId = labeler.taskId;
  if (!taskId) return;
  const task = labeler.task || {};
  const uncertain = Number(task.uncertain_count || 0);
  let confirmUncertain = false;
  if (uncertain > 0) {
    confirmUncertain = window.confirm(
      `有 ${uncertain} 张图片的结果不确定：请求可能已经发出并被供应商计费，但结果没来得及落库。`
      + "继续会重新打标这些图片，可能产生第二次费用。要继续吗？"
    );
    if (!confirmUncertain) {
      // 用户拒绝 = 只继续「明确未完成」的图片（跳过不确定项）。
      confirmUncertain = false;
    }
  }
  try {
    const payload = await api(`/api/labeler/tasks/${encodeURIComponent(taskId)}/retry`, {
      method: "POST",
      body: { mode: "unfinished", confirm_uncertain: confirmUncertain },
    });
    const next = payload.task || {};
    labeler.taskId = String(next.id || "");
    labeler.task = next;
    labeler.items = next.items || [];
    labeler.annotations = [];
    labeler.tagMatches = [];
    labeler.tags = [];
    labeler.sentences = [];
    setResultTab("images");
    renderTask(next);
    const skipped = Number(payload.skipped_succeeded || 0);
    const skippedUncertain = Number(payload.skipped_uncertain || 0);
    const notes = [];
    if (skipped) notes.push(`${skipped} 张已完成，未重复调用`);
    if (skippedUncertain) notes.push(`${skippedUncertain} 张结果不确定，本次跳过`);
    setHint(notes.join("；") || "已创建继续任务");
    await refreshBatches();
    startPolling();
  } catch (error) {
    setHint(`继续失败：${error.message}`, true);
  }
}

// ============================================================
// 批次列表与进度
// ============================================================

async function refreshBatches() {
  try {
    const { tasks } = await api("/api/labeler/tasks");
    labeler.batches = tasks || [];
  } catch (error) {
    setHint(`批次列表读取失败：${error.message}`, true);
    return;
  }
  // 刷新页面/重启应用后的恢复：当前没有选中批次时，自动接上最近一个还在跑的批次。
  if (!labeler.taskId) {
    const active = labeler.batches.find((task) => ["queued", "running"].includes(String(task.status || "")));
    if (active?.id) labeler.taskId = String(active.id);
  }
  renderBatches();
}

function renderBatches() {
  const list = $("#labelerBatchList");
  const empty = $("#labelerBatchEmpty");
  if (!list) return;
  if (!labeler.batches.length) {
    list.replaceChildren();
    if (empty) {
      empty.hidden = false;
      list.append(empty);
    }
    return;
  }
  if (empty) empty.hidden = true;
  const rows = labeler.batches.map((task) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "labeler-batch" + (task.id === labeler.taskId ? " active" : "");
    button.dataset.labelerTask = String(task.id || "");
    const top = document.createElement("div");
    top.className = "labeler-batch-top";
    const title = document.createElement("span");
    title.className = "labeler-batch-title";
    title.textContent = String(task.label || task.id || "批次");
    const state = document.createElement("span");
    state.className = "labeler-batch-state";
    state.textContent = STATUS_LABELS[task.status] || String(task.status || "");
    top.append(title, state);
    const meta = document.createElement("div");
    meta.className = "labeler-batch-meta";
    const progress = task.progress || {};
    const bits = [`${Number(progress.done || 0)}/${Number(progress.total || 0)} 完成`];
    if (Number(progress.failed || 0)) bits.push(`失败 ${progress.failed}`);
    if (Number(progress.uncertain || 0)) bits.push(`不确定 ${progress.uncertain}`);
    bits.push(String(task.preset || ""));
    meta.textContent = bits.filter(Boolean).join(" · ");
    button.append(top, meta);
    return button;
  });
  list.replaceChildren(...rows);
}

async function openBatch(taskId) {
  if (!taskId || taskId === labeler.taskId) return;
  stopPolling();
  labeler.taskId = taskId;
  labeler.annotations = [];
  labeler.tagMatches = [];
  labeler.tags = [];
  labeler.sentences = [];
  setResultTab("images");
  await reconcileTask();
  renderBatches();
}

function renderTask(task) {
  const status = String(task.status || "");
  const progress = task.progress || {};
  const total = Number(progress.total || 0);
  const done = Number(progress.done || 0);
  const failed = Number(progress.failed || 0);
  const uncertain = Number(progress.uncertain || 0);
  const cancelled = Number(progress.cancelled || 0);
  const finished = done + failed + uncertain + cancelled;
  const box = $("#labelerQueueStatus");
  if (box) box.hidden = false;
  const label = $("#labelerProgressLabel");
  if (label) label.textContent = STATUS_LABELS[status] || status || "准备中";
  const count = $("#labelerProgressCount");
  if (count) count.textContent = `${done} / ${total}`;
  const bar = $("#labelerProgressBar");
  if (bar) bar.style.width = total ? `${Math.min(100, Math.round((finished / total) * 100))}%` : "0";
  const note = $("#labelerProgressNote");
  if (note) {
    const bits = [];
    if (task.detail) bits.push(String(task.detail));
    if (cancelled) bits.push(`已取消 ${cancelled} 张`);
    if (task.usage && Object.keys(task.usage).length) {
      const tokens = Number(task.usage.total_tokens || 0);
      bits.push(tokens ? `累计 ${tokens} tokens（供应商读数）` : "已记录供应商用量");
    } else if (done) {
      bits.push("供应商未返回用量，费用未知");
    }
    note.textContent = bits.join("；");
  }
  const cancelBtn = $("#labelerCancel");
  if (cancelBtn) cancelBtn.hidden = !["queued", "running"].includes(status);
  const continueBtn = $("#labelerContinue");
  if (continueBtn) continueBtn.hidden = !task.can_continue;
  renderResultBody();
}

// ============================================================
// 结果三档
// ============================================================

function setResultTab(tab) {
  const next = ["images", "tags", "sentences"].includes(tab) ? tab : "images";
  labeler.resultTab = next;
  labeler.activeTag = "";
  $$("#studioPageLabeler .labeler-result-tab").forEach((btn) => {
    const active = btn.dataset.labelerResult === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  renderResultBody();
  if (next === "tags") void ensureTagView();
  if (next === "sentences") void loadSentences();
}

/** 按标签视图：确保既有标签统计，也有可筛选的标注集合（当前批次或全库）。 */
async function ensureTagView() {
  await loadTags();
  if (!labeler.annotations.length) {
    try {
      const query = labeler.taskId ? `task_id=${encodeURIComponent(labeler.taskId)}&` : "";
      const { annotations } = await api(`/api/labeler/annotations?${query}limit=1000`);
      labeler.annotations = annotations || [];
    } catch (error) {
      setHint(`标注读取失败：${error.message}`, true);
    }
  }
  renderResultBody();
}

async function loadTaskResults(taskId) {
  try {
    const { annotations } = await api(
      `/api/labeler/annotations?task_id=${encodeURIComponent(taskId)}&limit=2000`
    );
    labeler.annotations = annotations || [];
  } catch (error) {
    setHint(`标注结果读取失败：${error.message}`, true);
    labeler.annotations = [];
  }
  if (labeler.resultTab === "tags") await loadTags();
  if (labeler.resultTab === "sentences") await loadSentences();
  renderResultBody();
}

async function loadTags() {
  try {
    const { tags } = await api("/api/labeler/tags?limit=300");
    labeler.tags = tags || [];
  } catch (error) {
    setHint(`标签统计读取失败：${error.message}`, true);
    labeler.tags = [];
  }
  renderResultBody();
}

async function loadSentences() {
  try {
    const query = labeler.search ? `&q=${encodeURIComponent(labeler.search)}` : "";
    const payload = await api(`/api/labeler/sentences/stats?limit=300${query}`);
    labeler.sentences = payload.items || [];
    renderResultBody(payload);
  } catch (error) {
    setHint(`句子库读取失败：${error.message}`, true);
    labeler.sentences = [];
    renderResultBody();
  }
}

function annotationForItem(item) {
  const id = Number(item.annotation_id || 0);
  if (!id) return null;
  return labeler.annotations.find((row) => Number(row.id) === id) || null;
}

function matchesSearch(row) {
  const needle = labeler.search.trim().toLowerCase();
  if (!needle) return true;
  const haystack = [
    row.name, row.relative_path, row.description, ...(row.tags || []),
  ].join(" ").toLowerCase();
  return haystack.includes(needle);
}

function renderResultBody(sentencePayload = null) {
  const body = $("#labelerResultBody");
  if (!body) return;
  if (labeler.resultTab === "tags") {
    renderTagCloud(body);
    return;
  }
  if (labeler.resultTab === "sentences") {
    renderSentences(body, sentencePayload);
    return;
  }
  renderImageResults(body);
}

function renderImageResults(body) {
  if (!labeler.taskId) {
    body.replaceChildren(emptyPanel("还没有结果", "拖入文件夹、完成上传并点「开始打标」后，结果会按图片显示在这里。"));
    return;
  }
  const annotationById = new Map(labeler.annotations.map((row) => [Number(row.id), row]));
  const rows = [];
  for (const item of labeler.items) {
    const annotation = Number(item.annotation_id || 0)
      ? annotationById.get(Number(item.annotation_id))
      : null;
    const probe = {
      name: item.name, relative_path: item.relative_path,
      description: annotation?.description || "", tags: annotation?.tags || [],
    };
    if (labeler.search && !matchesSearch(probe)) continue;
    rows.push({ item, annotation });
  }
  if (!rows.length) {
    body.replaceChildren(emptyPanel(
      labeler.search ? "没有匹配的图片" : "还没有逐图结果",
      labeler.search ? "换个关键词试试。" : "批次开始后，每处理完一张就会出现在这里。"
    ));
    return;
  }
  body.replaceChildren(...rows.map(({ item, annotation }) => {
    const card = document.createElement("article");
    card.className = "labeler-anno";
    const head = document.createElement("div");
    head.className = "labeler-anno-head";
    const name = document.createElement("span");
    name.className = "labeler-anno-name";
    name.textContent = item.name || item.relative_path || item.item_id;
    const state = document.createElement("span");
    state.className = "labeler-anno-state";
    const label = ITEM_STATE_LABELS[item.state] || item.state;
    state.textContent = label;
    if (["failed", "uncertain"].includes(item.state)) state.classList.add("failed");
    head.append(name, state);
    card.append(head);
    if (item.relative_path && item.relative_path !== item.name) {
      const path = document.createElement("span");
      path.className = "labeler-anno-path";
      path.textContent = item.relative_path;
      card.append(path);
    }
    if (annotation?.tags?.length) {
      card.append(tagList(annotation.tags, annotation.confidences));
    }
    if (annotation?.description) {
      const desc = document.createElement("p");
      desc.className = "labeler-anno-desc";
      desc.textContent = annotation.description;
      card.append(desc);
    }
    if (item.error) {
      const err = document.createElement("p");
      err.className = "labeler-anno-desc";
      err.textContent = `失败原因：${item.error}`;
      card.append(err);
    }
    return card;
  }));
}

function tagList(tags, confidences) {
  const wrap = document.createElement("div");
  wrap.className = "labeler-taglist";
  tags.forEach((tag, index) => {
    const span = document.createElement("span");
    span.className = "labeler-tag";
    span.textContent = String(tag);
    const conf = Number((confidences || [])[index]);
    if (Number.isFinite(conf) && conf >= 0) {
      const em = document.createElement("em");
      em.textContent = conf.toFixed(2);
      span.append(em);
    }
    wrap.append(span);
  });
  return wrap;
}

function renderTagCloud(body) {
  const needle = labeler.search.trim().toLowerCase();
  const tags = needle
    ? labeler.tags.filter((row) => String(row.tag).toLowerCase().includes(needle))
    : labeler.tags;
  if (!tags.length) {
    body.replaceChildren(emptyPanel(
      needle ? "没有匹配的标签" : "还没有标签",
      needle ? "换个关键词试试。" : "完成一批打标后，这里会按出现次数列出全部标签。"
    ));
    return;
  }
  const cloud = document.createElement("div");
  cloud.className = "labeler-tagcloud";
  for (const row of tags) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "labeler-tag" + (row.tag === labeler.activeTag ? " active" : "");
    button.textContent = `${row.tag} ×${row.count}`;
    button.dataset.labelerTag = row.tag;
    cloud.append(button);
  }
  body.replaceChildren(cloud);
  const matched = labeler.activeTag
    ? labeler.tagMatches.filter((row) => (row.tags || []).includes(labeler.activeTag))
    : [];
  if (matched.length) {
    body.append(...matched.map((row) => {
      const card = document.createElement("article");
      card.className = "labeler-anno";
      const head = document.createElement("div");
      head.className = "labeler-anno-head";
      const name = document.createElement("span");
      name.className = "labeler-anno-name";
      name.textContent = row.relative_path || row.name;
      head.append(name);
      card.append(head, tagList(row.tags || [], row.confidences || []));
      if (row.description) {
        const desc = document.createElement("p");
        desc.className = "labeler-anno-desc";
        desc.textContent = row.description;
        card.append(desc);
      }
      return card;
    }));
  }
}

function renderSentences(body, payload) {
  if (payload?.degraded) {
    const note = document.createElement("p");
    note.className = "labeler-degraded";
    note.textContent = payload.note || "规则分句，结果仅供参考。";
    body.replaceChildren(note);
  }
  if (!labeler.sentences.length) {
    if (!payload?.degraded) {
      body.replaceChildren(emptyPanel("还没有句子", "描述类输出会进入句子库；只打标签时这里为空。"));
    }
    return;
  }
  const list = document.createElement("div");
  for (const row of labeler.sentences) {
    const item = document.createElement("div");
    item.className = "labeler-sentence";
    const count = document.createElement("b");
    count.textContent = `×${row.count}`;
    const text = document.createElement("span");
    text.textContent = row.sentence;
    item.append(count, text);
    list.append(item);
  }
  body.append(list);
}

function emptyPanel(title, hint) {
  const wrap = document.createElement("div");
  wrap.className = "labeler-empty";
  const strong = document.createElement("p");
  strong.textContent = title;
  strong.style.margin = "0 0 4px";
  strong.style.color = "var(--text)";
  const text = document.createElement("p");
  text.textContent = hint;
  text.style.margin = "0";
  wrap.append(strong, text);
  return wrap;
}

// ============================================================
// 导出
// ============================================================

async function exportResults() {
  const format = $("#labelerExportFormat")?.value || "json";
  const body = { format };
  if (labeler.taskId) body.task_id = labeler.taskId;
  else if (labeler.search) body.q = labeler.search;
  const button = $("#labelerExport");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/labeler/export", { method: "POST", body });
    if (payload?.path) {
      triggerDownload(fileUrl(payload.path));
      setHint(`已生成导出文件：${payload.name}（${payload.count} 条）`);
    }
  } catch (error) {
    setHint(`导出失败：${error.message}`, true);
  } finally {
    if (button) button.disabled = false;
  }
}

// ============================================================
// 事件接线
// ============================================================

export function bindLabelerEvents() {
  const dropzone = $("#labelerDropzone");
  const folderInput = $("#labelerFolderInput");
  if (dropzone) {
    // 打标页的拖放必须吃掉事件：应用级/聊天区的 drop handler 会先抢走它。
    for (const name of ["dragenter", "dragover"]) {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        event.stopPropagation();
        dropzone.classList.add("dragover");
      });
    }
    for (const name of ["dragleave", "dragend"]) {
      dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        event.stopPropagation();
        dropzone.classList.remove("dragover");
      });
    }
    dropzone.addEventListener("drop", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      dropzone.classList.remove("dragover");
      try {
        const picked = await collectDropFiles(event);
        await acceptFiles(picked);
      } catch (error) {
        setPickStatus(`读取拖入内容失败：${error.message}`);
      }
    });
    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        folderInput?.click();
      }
    });
    dropzone.addEventListener("click", () => folderInput?.click());
  }
  // 应用级 drop 兜底：拖到打标页其它位置时不要让浏览器直接打开图片。
  const page = $("#studioPageLabeler");
  if (page) {
    page.addEventListener("dragover", (event) => {
      if (event.dataTransfer?.types?.includes("Files")) event.preventDefault();
    });
  }
  folderInput?.addEventListener("change", () => {
    const files = [...(folderInput.files || [])];
    folderInput.value = "";
    void acceptFiles(files.map((file) => ({
      file, fullPath: file.webkitRelativePath || file.name,
    })));
  });
  $("#labelerClearSelected")?.addEventListener("click", () => {
    abortUploads();
    labeler.selected = [];
    setPickStatus("");
    updateSelectedUi();
  });
  $("#labelerRetryUpload")?.addEventListener("click", () => { void uploadAll({ retryFailed: true }); });
  $("#labelerPreset")?.addEventListener("change", () => {
    labeler.presetId = String($("#labelerPreset")?.value || "");
    renderPresetDescription();
  });
  $("#labelerProviderSelect")?.addEventListener("change", () => {
    labeler.providerKey = String($("#labelerProviderSelect")?.value || "");
    labeler.modelName = "";
    updateCostNote();
    void loadModelOptions();
  });
  $("#labelerModelSelect")?.addEventListener("change", () => {
    labeler.modelName = String($("#labelerModelSelect")?.value || "");
  });
  $("#labelerStart")?.addEventListener("click", () => { void startTask(); });
  $("#labelerCancel")?.addEventListener("click", () => { void cancelTask(); });
  $("#labelerContinue")?.addEventListener("click", () => { void continueTask(); });
  $("#labelerRefreshRecent")?.addEventListener("click", () => { void refreshBatches(); });
  $("#labelerBatchList")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-labeler-task]");
    if (!button) return;
    void openBatch(button.dataset.labelerTask);
  });
  $("#labelerPanes")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-labeler-pane]");
    if (!btn) return;
    setLabelerPane(btn.dataset.labelerPane);
  });
  $("#studioPageLabeler .labeler-result-tabs")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-labeler-result]");
    if (!btn) return;
    setResultTab(btn.dataset.labelerResult);
  });
  $("#labelerResultBody")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-labeler-tag]");
    if (!btn) return;
    labeler.activeTag = labeler.activeTag === btn.dataset.labelerTag ? "" : btn.dataset.labelerTag;
    if (labeler.activeTag) void loadAnnotationsForTag(labeler.activeTag);
    renderResultBody();
  });
  $("#labelerSearch")?.addEventListener("input", () => {
    labeler.search = String($("#labelerSearch")?.value || "");
    if (labeler.searchTimer) clearTimeout(labeler.searchTimer);
    labeler.searchTimer = setTimeout(() => {
      if (labeler.resultTab === "sentences") void loadSentences();
      else renderResultBody();
    }, SEARCH_DEBOUNCE_MS);
  });
  $("#labelerExport")?.addEventListener("click", () => { void exportResults(); });

  // 功能页切换：切走暂停轮询，切回来按服务端校准。
  onStudioPageChange((next, detail) => {
    if (detail?.closed || next !== "labeler") {
      stopPolling();
      return;
    }
    void activateLabeler({ refresh: true });
  });
}

async function loadAnnotationsForTag(tag) {
  try {
    const { annotations } = await api(
      `/api/labeler/annotations?q=${encodeURIComponent(tag)}&limit=500`
    );
    labeler.tagMatches = annotations || [];
  } catch (error) {
    setHint(`按标签读取失败：${error.message}`, true);
    labeler.tagMatches = [];
  }
  renderResultBody();
}
