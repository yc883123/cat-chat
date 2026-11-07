// ============================================================
// 23-labeler.js —— 工作台「打标器」功能页（docs/plans/2026-10-23-打标器接入工作台计划.md）
// 批量打标（模型来自设置里的 provider，云端与本地后端都算）：
// 文件夹拖入 → 逐批上传到受管 uploads → 提交批次 → 逐图进度 →
// 结果三档（按图 / 按标签 / 句子库）→ 导出 JSON / CSV / .txt。
// 与聊天域零耦合；只调 /api/labeler/*（含打标专用上传）。
// ============================================================

import { $, $$, api, applyModelSkinFor, state, toast, triggerDownload } from "./01-core.js";
import { fileUrl, isPywebview } from "./03-media.js";
import { currentStudioPage, onStudioPageChange } from "./22-studio.js";

// ---- 与服务端一致的批量上限（前端只做提前提示，服务端仍逐项校验） ----
const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".webp", ".gif"]);
const MAX_BATCH_IMAGES = 9999;
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const MAX_SOURCE_IMAGE_BYTES = 100 * 1024 * 1024;
const MAX_BATCH_BYTES = 20 * 1024 * 1024 * 1024;
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
// 终态批次才可删除；排队/打标中的先取消，等结束再删（打标按图计费，取消必须显式）。
const TERMINAL_BATCH_STATUSES = new Set(["done", "failed", "cancelled", "interrupted"]);
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
  imageResultLimit: 500,
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
  // 预设打标 / 自定义打标 两页签（写进 #studioPageLabeler 的 data-labeler-page）。
  page: "preset",
  // 自定义档选中的 Skill（选中即走对话链路打标，engine=chat + skill_id 提交，
  // 正文不进提示词框）：{id, name} | null
  skillPick: null,
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
      row.status === "failed" ? "上传失败" : row.status === "uploading" ? (row.processing ? "自动压缩中" : `${row.progress || 0}%`) : "待上传"
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
    setHint(`生成模式读取失败：${error.message}`, true);
  }
}

function renderPresetDescription() {
  const node = $("#labelerPresetDesc");
  if (!node) return;
  const preset = labeler.presets.find((row) => row.id === labeler.presetId);
  if (!preset) {
    node.textContent = "";
    return;
  }
  // 口径固定为「选什么就忽略什么」：先把要忽略的写清楚，再补说明。
  node.textContent = preset.ignored
    ? `描述会忽略：${preset.ignored}。${preset.description || ""}`
    : (preset.description || "");
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
      setLabelerPage(labeler.page);
      // 先渲染一次结果区：三档说明行不依赖批次存在，进页就能看到「按图」是什么。
      renderResultBody();
      await ensurePresets();
      await ensureModels();
      void ensureSkills();
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
// 预设打标 / 自定义打标 两页签
// ============================================================

function currentLabelerPage() {
  return $("#studioPageLabeler")?.dataset.labelerPage === "custom" ? "custom" : "preset";
}

function setLabelerPage(page) {
  const next = page === "custom" ? "custom" : "preset";
  labeler.page = next;
  const page2 = $("#studioPageLabeler");
  if (page2) page2.dataset.labelerPage = next;
  $$("#labelerPanes .labeler-page-btn").forEach((btn) => {
    const active = btn.dataset.labelerPageTab === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

// 自定义页的 Skill 下拉：与聊天同一份列表（/api/skills/scan），只做展示与选用入口。
function skillPlaceholderOption() {
  const option = document.createElement("option");
  option.value = "";
  option.textContent = "不用 Skill（手输规则）";
  return option;
}

async function ensureSkills() {
  const select = $("#labelerSkillSelect");
  if (!select || select.options.length) return;
  try {
    // 与聊天同一份接口：POST 空体（GET 会 404，见 15-bind-events.js 同款调用）。
    const payload = await api("/api/skills/scan", { method: "POST", body: {} });
    const skills = (payload.skills || []).filter((row) => row && row.id);
    select.replaceChildren(
      skillPlaceholderOption(),
      ...skills.map((skill) => {
        const option = document.createElement("option");
        option.value = String(skill.id || "");
        option.textContent = String(skill.name || skill.id);
        return option;
      }),
    );
  } catch (error) {
    select.replaceChildren(skillPlaceholderOption());
    setSkillNote(`Skill 列表读取失败：${error.message}`, true);
  }
}

function setSkillNote(text, isError = false) {
  const node = $("#labelerSkillNote");
  if (!node) return;
  node.textContent = String(text || "");
  node.classList.toggle("error", Boolean(isError));
}

// 自定义档的规则来源切换：选了 Skill 走对话链路（engine=chat，输出规则由 Skill 决定），
// 没选就走普通单轮打标（手输提示词即规则）。输出形式/语言两行只在手输模式下有意义。
function syncCustomEngineUi() {
  const picked = Boolean(labeler.skillPick?.id);
  const outputRow = $("#labelerCustomOutputRow");
  if (outputRow) outputRow.hidden = picked;
}

// 选中即生效：只记录选择，不取正文不显示（对话链路里模型自己按需读 Skill 文件）。
function applySelectedSkill() {
  const select = $("#labelerSkillSelect");
  const id = String(select?.value || "").trim();
  if (!id) {
    labeler.skillPick = null;
    setSkillNote("未使用 Skill：用提示词框里的手输内容当打标规则（普通单轮打标）。");
    syncCustomEngineUi();
    return;
  }
  const name = String(select.selectedOptions?.[0]?.textContent || id);
  labeler.skillPick = { id, name };
  setSkillNote(
    `已选 Skill「${name}」：提交后走对话同款链路打标（模型按 Skill 规则工作、可自行读取其参考文件，`
    + `不做任何截断），每张图可能多轮工具调用，耗时与费用高于普通打标；输出形式与语言由 Skill 决定。`,
  );
  syncCustomEngineUi();
}

function clearSystemPrompt() {
  const textarea = $("#labelerCustomSystemPrompt");
  if (textarea) textarea.value = "";
  const select = $("#labelerSkillSelect");
  if (select) select.value = "";
  labeler.skillPick = null;
  setSkillNote("已清空手输提示词，并停用已选 Skill（回到普通手输打标）。");
  syncCustomEngineUi();
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
    if (row.file.size > MAX_SOURCE_IMAGE_BYTES) {
      setPickStatus(`「${row.file.name}」超过上传传输上限 ${formatBytes(MAX_SOURCE_IMAGE_BYTES)}，无法接收`);
      return;
    }
  }
  // 替换旧选择：一次拖入 = 一个批次；重复拖入不会把两批混在一起。
  abortUploads();
  labeler.selected = images.map((row, index) => ({
    id: `${Date.now()}-${index}`,
    file: row.file,
    name: row.file.name,
    relative_path: normalizeRelativePath(row.fullPath, row.file.name),
    size: row.file.size,
    cacheSize: 0,
    status: "pending",
    progress: 0,
    path: "",
    thumb_path: "",
    original_sha256: "",
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
    row.processing = row.file.size > MAX_IMAGE_BYTES;
    row.progress = 0;
    row.error = "";
    if (row.processing) setPickStatus(`正在自动压缩「${row.name}」的缓存副本…原图会保留用于导出`);
    // 打标专用上传：原图留存用于训练包导出，缓存副本按运行设置压缩。
    xhr.open("POST", "/api/labeler/uploads");
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
        row.original_sha256 = String(payload.original_sha256 || "");
        row.cacheSize = Number(payload.size || 0);
        row.progress = 100;
        // 服务端落盘名以返回为准（含安全字符清洗）。
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
// 配置收集 / 提交
// ============================================================

// 输出形式 ↔ 输出语言联动（2026-10-26 计划）：语言选项只显示当前形式下
// 有明确含义的；「英文标签 + 中文描述（mixed）」只在「标签 + 自然语言」出现，
// 文案随之切换（全中文 / 全英文），隐藏值绝不保留生效。
const LABELER_LANGUAGE_LABELS = {
  single: { zh: "中文", en: "英文" },
  both: { zh: "全中文", en: "全英文", mixed: "英文标签 + 中文描述" },
};

function syncLanguageOptions() {
  const modeSelect = $("#labelerOutputMode");
  const langSelect = $("#labelerLanguage");
  if (!modeSelect || !langSelect) return;
  const mode = String(modeSelect.value || "keywords");
  const labels = mode === "both" ? LABELER_LANGUAGE_LABELS.both : LABELER_LANGUAGE_LABELS.single;
  for (const option of Array.from(langSelect.options)) {
    const value = String(option.value || "");
    option.hidden = !(value in labels);
    if (value in labels) option.textContent = labels[value];
  }
  // 语言值必须始终映射到当前可见选项：mixed 在单一形式下按计划规则映射
  // （切到「标签」选英文，切到「词组短语 / 自然语言」选中文）。
  if (!(String(langSelect.value || "") in labels)) {
    langSelect.value = mode === "keywords" ? "en" : "zh";
  }
}

function collectConfig() {
  const providerSelect = $("#labelerProviderSelect");
  const modelSelect = $("#labelerModelSelect");
  const config = {
    engine: "provider",
    output_mode: $("#labelerOutputMode")?.value || "keywords",
    language: $("#labelerLanguage")?.value || "mixed",
    preset: $("#labelerPreset")?.value || labeler.presetId || "",
    model_key: String(providerSelect?.value || ""),
    model_name: String(modelSelect?.value || ""),
    confidence_threshold: Number($("#labelerConfidence")?.value || 0.35),
    max_tags: Number($("#labelerMaxTags")?.value || 24),
    custom_prompt: String($("#labelerCustomPrompt")?.value || "").trim(),
    trigger_word: {
      enabled: Boolean($("#labelerTriggerEnabled")?.checked),
      value: String($("#labelerTriggerWord")?.value || "").trim(),
    },
    concurrency: Number($("#labelerConcurrency")?.value || 1),
    retry_limit: Number($("#labelerRetryLimit")?.value || 2),
    timeout_seconds: Number($("#labelerTimeout")?.value || 90),
    prompt_version: 2,
  };
  if (currentLabelerPage() === "custom") {
    // 自定义档：规则来源二选一——选了 Skill 走对话链路（engine=chat + skill_id，
    // 输出形式/语言/手输提示词都不参与）；没选 Skill 就是手输提示词（键在 = 自定义档），
    // 输出形式/语言默认「以提示词为准」（auto）= 不追加规则；预设固定 general 只做噪声清理。
    config.output_mode = $("#labelerCustomOutputMode")?.value || "auto";
    config.language = $("#labelerCustomLanguage")?.value || "auto";
    config.preset = "general";
    config.custom_prompt = "";
    if (labeler.skillPick?.id) {
      config.engine = "chat";
      config.skill_id = labeler.skillPick.id;
    } else {
      config.system_prompt = String($("#labelerCustomSystemPrompt")?.value || "").trim();
    }
  }
  return config;
}

async function startTask() {
  const uploaded = labeler.selected.filter((row) => row.status === "uploaded" && row.path);
  if (!uploaded.length) {
    setHint("请先拖入文件夹并等待图片上传完成", true);
    return;
  }
  const totalBytes = uploaded.reduce((sum, row) => sum + Number(row.cacheSize || 0), 0);
  if (totalBytes > MAX_BATCH_BYTES) {
    setHint(`处理后缓存合计 ${formatBytes(totalBytes)}，超过 ${formatBytes(MAX_BATCH_BYTES)} 上限，请拆分批次`, true);
    return;
  }
  const config = collectConfig();
  if (!config.model_key || !config.model_name) {
    setHint("请选择模型配置与模型", true);
    return;
  }
  if (currentLabelerPage() === "custom" && !labeler.skillPick?.id
      && !String(config.system_prompt || "").trim()) {
    setHint("自定义打标要先选一个 Skill，或在系统提示词框手输规则", true);
    return;
  }
  if (config.trigger_word.enabled && !config.trigger_word.value) {
    setHint("启用触发词后，请先填写触发词内容", true);
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
          cache_size: row.cacheSize,
          original_sha256: row.original_sha256 || "",
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
    labeler.imageResultLimit = 500;
    renderTask(task);
    if (!["queued", "running"].includes(String(task.status || ""))) {
      stopPolling();
      await loadTaskItems(taskId);
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
    labeler.imageResultLimit = 500;
    renderTask(task);
    if (["queued", "running"].includes(String(task.status || ""))) startPolling();
    else {
      stopPolling();
      await loadTaskItems(taskId);
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
  return retryTask("unfinished");
}

async function retryFailedTask() {
  return retryTask("failed_only");
}

async function retryTask(mode) {
  const taskId = labeler.taskId;
  if (!taskId) return;
  const task = labeler.task || {};
  const uncertain = mode === "unfinished" ? Number(task.uncertain_count || 0) : 0;
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
      body: { mode, confirm_uncertain: confirmUncertain },
    });
    const next = payload.task || {};
    labeler.taskId = String(next.id || "");
    labeler.task = next;
    labeler.items = next.items || [];
    labeler.annotations = [];
    labeler.annotationTotal = 0;
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
    setHint(notes.join("；") || (mode === "failed_only" ? "已创建失败图片重试任务" : "已创建继续任务"));
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
    // 行容器：主按钮（选批次）+ 终态批次的删除按钮，不能是按钮套按钮。
    const row = document.createElement("div");
    row.className = "labeler-batch" + (task.id === labeler.taskId ? " active" : "");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "labeler-batch-main";
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
    // 批次历史显示用户能看懂的生成模式名，不暴露 face_lora 这类内部 ID；
    // 自定义打标的任务没有预设，显示成「自定义」。
    const preset = labeler.presets.find((row2) => row2.id === task.preset);
    bits.push(preset?.label || (String(task.preset || "") === "custom" ? "自定义" : String(task.preset || "")));
    meta.textContent = bits.filter(Boolean).join(" · ");
    button.append(top, meta);
    row.append(button);
    if (TERMINAL_BATCH_STATUSES.has(String(task.status || ""))) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "labeler-batch-delete";
      del.dataset.labelerDelete = String(task.id || "");
      del.textContent = "删除";
      del.title = "删除这条批次记录（已生成的标注仍保留在标注库，可继续查询和导出）";
      row.append(del);
    }
    return row;
  });
  list.replaceChildren(...rows);
}

// 删除的是批次记录与逐图运行行；标注库按「图片内容 + 配置」全局复用，不受影响。
async function deleteBatchTask(taskId) {
  const id = String(taskId || "");
  if (!id) return;
  const row = labeler.batches.find((item) => String(item.id) === id);
  if (!window.confirm(
    `删除批次「${row?.label || id}」？\n已生成的标注仍保留在标注库，可继续查询和导出。`,
  )) return;
  try {
    const payload = await api("/api/labeler/tasks", { method: "DELETE", body: { task_ids: [id] } });
    if (Array.isArray(payload.skipped) && payload.skipped.length) {
      toast("这条批次还在排队或打标中，没有删除；请先取消，等它结束后再删");
    } else {
      toast("已删除这条批次记录（标注库不受影响）");
    }
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
  if (labeler.taskId === id) clearSelectedBatch();
  await refreshBatches();
}

async function clearFinishedBatches() {
  const finished = labeler.batches.filter(
    (row) => TERMINAL_BATCH_STATUSES.has(String(row.status || "")));
  if (!finished.length) {
    toast("没有已结束的批次可清理");
    return;
  }
  if (!window.confirm(
    `清理 ${finished.length} 条已结束的批次记录？\n排队或打标中的批次不会动；已生成的标注仍保留在标注库，可继续查询和导出。`,
  )) return;
  try {
    const payload = await api("/api/labeler/tasks", { method: "DELETE", body: {} });
    const skipped = Array.isArray(payload.skipped) ? payload.skipped.length : 0;
    toast(`已清理 ${Number(payload.deleted || 0)} 条批次记录${skipped ? `，${skipped} 条还在执行已跳过` : ""}`);
  } catch (error) {
    toast(`清理失败：${error.message}`);
  }
  if (finished.some((row) => String(row.id) === labeler.taskId)) clearSelectedBatch();
  await refreshBatches();
}

// 清空整个标注库（用户显式操作）：标注全删、复用缓存失效（同图重打会重新计费）；
// 批次记录不动；原图留存由启动恢复的 prune 链路按保护窗口自然回收。
async function wipeAnnotations() {
  if (!window.confirm(
    "清空整个标注库？\n所有图的标签与描述都会删除，按标签 / 句子库会清空；\n同样的图再打标会重新调用模型、重新计费；\n原图留存失去保护，之后会按缓存策略自动回收。",
  )) return;
  const button = $("#labelerWipeAnnotations");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/labeler/annotations", { method: "DELETE", body: {} });
    toast(`已清空标注库（${Number(payload.deleted || 0)} 条标注）`);
  } catch (error) {
    toast(`清空失败：${error.message}`);
    return;
  } finally {
    if (button) button.disabled = false;
  }
  labeler.annotations = [];
  labeler.tagMatches = [];
  labeler.tags = [];
  labeler.sentences = [];
  // 选中批次还在：逐图行的 annotation_id 已被服务端置空，按服务端详情校准一遍。
  if (labeler.taskId) await reconcileTask();
  else renderResultBody();
}

// 正在查看的批次被删掉时：停轮询、清选择与结果缓存，界面回到未选批次的空态。
function clearSelectedBatch() {
  stopPolling();
  labeler.taskId = "";
  labeler.task = null;
  labeler.items = [];
  labeler.annotations = [];
  labeler.annotationTotal = 0;
  labeler.tagMatches = [];
  labeler.tags = [];
  labeler.sentences = [];
  const box = $("#labelerQueueStatus");
  if (box) box.hidden = true;
  renderResultBody();
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
  const failedBtn = $("#labelerRetryModelFailed");
  if (failedBtn) failedBtn.hidden = !task.can_retry_failed;
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

async function loadTaskItems(taskId, { reset = false } = {}) {
  if (reset) labeler.items = [];
  const pageSize = 500;
  const offset = labeler.items.length;
  try {
    const payload = await api(`/api/labeler/tasks/${encodeURIComponent(taskId)}/items?offset=${offset}&limit=${pageSize}`);
    if (labeler.taskId !== taskId) return;
    labeler.items.push(...(payload.items || []));
    labeler.task = { ...(labeler.task || {}), items_total: Number(payload.total || 0) };
    renderResultBody();
  } catch (error) {
    setHint(`图片列表读取失败：${error.message}`, true);
  }
}

async function loadNextTaskItems() {
  return loadTaskItems(labeler.taskId);
}

async function loadTaskResults(taskId) {
  try {
    const payload = await api(`/api/labeler/annotations?task_id=${encodeURIComponent(taskId)}&limit=500&offset=0`);
    if (labeler.taskId !== taskId) return;
    labeler.annotations = payload.annotations || [];
    labeler.annotationTotal = Number(payload.total || 0);
  } catch (error) {
    setHint(`标注结果读取失败：${error.message}`, true);
    labeler.annotations = [];
    labeler.annotationTotal = 0;
  }
  if (labeler.resultTab === "tags") await loadTags();
  if (labeler.resultTab === "sentences") await loadSentences();
  renderResultBody();
}

async function loadNextAnnotations() {
  const taskId = labeler.taskId;
  if (!taskId || labeler.annotations.length >= Number(labeler.annotationTotal || 0)) return;
  const offset = labeler.annotations.length;
  try {
    const payload = await api(`/api/labeler/annotations?task_id=${encodeURIComponent(taskId)}&limit=500&offset=${offset}`);
    if (labeler.taskId !== taskId) return;
    labeler.annotations.push(...(payload.annotations || []));
    renderResultBody();
  } catch (error) {
    setHint(`更多标注读取失败：${error.message}`, true);
  }
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

// 三档结果视图的一句话说明：随当前标签显示在结果区头部（用户口径，不讲内部实现）。
const RESULT_TAB_HINTS = {
  images: "当前批次的逐图结果，跟着批次走——批次记录删除后这里会清空；成果仍在标注库，可在「按标签」「句子库」查看或随时导出。",
  tags: "标注库里全部标签的出现次数，跨批次；点标签可筛出带它的图。",
  sentences: "描述类输出拆句去重的统计，跨批次；×N 越高说明这句话被重复得越多。",
};

function renderResultBody(sentencePayload = null) {
  const body = $("#labelerResultBody");
  if (!body) return;
  const hint = $("#labelerResultsHint");
  if (hint) hint.textContent = RESULT_TAB_HINTS[labeler.resultTab] || "";
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
    body.replaceChildren(emptyPanel("还没有结果", "当前没有选中批次：按图跟的是批次，批次记录删除后这里会清空；标注库里的成果仍可在「按标签」「句子库」查看或随时导出。"));
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
  const visible = rows.slice(0, labeler.imageResultLimit);
  const cards = visible.map(({ item, annotation }) => {
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
  });
  body.replaceChildren(...cards);
  const hasMoreItems = labeler.items.length < Number(labeler.task?.items_total || labeler.items.length);
  const hasMoreAnnotations = labeler.annotations.length < Number(labeler.annotationTotal || labeler.annotations.length);
  if (hasMoreItems || hasMoreAnnotations) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "control-button tiny";
    more.textContent = hasMoreItems
      ? `加载更多图片（${labeler.items.length}/${labeler.task?.items_total || 0}）`
      : `加载更多标注（${labeler.annotations.length}/${labeler.annotationTotal || 0}）`;
    more.addEventListener("click", () => {
      if (hasMoreItems) void loadNextTaskItems();
      else void loadNextAnnotations();
    });
    body.append(more);
  }
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
  const format = $("#labelerExportFormat")?.value || "captions";
  const body = { format };
  if (labeler.taskId) body.task_id = labeler.taskId;
  else if (labeler.search) body.q = labeler.search;
  // 包名可选项：填了就叫这个名字；留空由服务端自动命名（同一文件夹时
  // 直接用文件夹本来的名字，多个包一眼分清谁是谁的数据集）。
  const customName = String($("#labelerExportName")?.value || "").trim();
  if (customName) body.name = customName;
  const button = $("#labelerExport");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/labeler/export", { method: "POST", body });
    if (payload?.path) {
      const noun = format === "lora" ? "张" : "条";
      if (isPywebview()) {
        // 桌面端不下载副本：WebView2 不处理 <a download>，点了也没反应；
        // 文件已经在应用数据目录里，直接告诉用户位置，交给「打开文件夹」。
        setHint(`已导出 ${payload.count} ${noun}：${payload.name}，位置：${payload.path}`);
      } else {
        triggerDownload(fileUrl(payload.path));
        setHint(`已导出 ${payload.count} ${noun}：${payload.name}（已开始下载）`);
      }
    }
  } catch (error) {
    setHint(`导出失败：${error.message}`, true);
  } finally {
    if (button) button.disabled = false;
  }
}

async function openExportFolder() {
  const button = $("#labelerOpenExports");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/labeler/exports/open", { method: "POST", body: {} });
    setHint(payload?.path ? `已打开导出文件夹：${payload.path}` : "已打开导出文件夹");
  } catch (error) {
    setHint(`无法打开导出文件夹：${error.message}`, true);
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
    // 打标器换模型也跟随皮肤（规则与主对话一致：自动模式才生效，识别不出家族不换）。
    void applyModelSkinFor(labeler.modelName);
  });
  // 输出形式变化 → 同步输出语言的可见选项与有效值（隐藏值不保留生效）。
  $("#labelerOutputMode")?.addEventListener("change", syncLanguageOptions);
  syncLanguageOptions();
  // 触发词开关：打开才可填；提交前 startTask 会校验「启用必须已填写」。
  const triggerEnabled = $("#labelerTriggerEnabled");
  const triggerWord = $("#labelerTriggerWord");
  triggerEnabled?.addEventListener("change", () => {
    if (triggerWord) triggerWord.disabled = !triggerEnabled.checked;
  });
  $("#labelerStart")?.addEventListener("click", () => { void startTask(); });
  $("#labelerCancel")?.addEventListener("click", () => { void cancelTask(); });
  $("#labelerContinue")?.addEventListener("click", () => { void continueTask(); });
  $("#labelerRetryModelFailed")?.addEventListener("click", () => { void retryFailedTask(); });
  $("#labelerRefreshRecent")?.addEventListener("click", () => { void refreshBatches(); });
  $("#labelerClearFinished")?.addEventListener("click", () => { void clearFinishedBatches(); });
  $("#labelerBatchList")?.addEventListener("click", (event) => {
    const del = event.target.closest("[data-labeler-delete]");
    if (del) {
      void deleteBatchTask(del.dataset.labelerDelete);
      return;
    }
    const button = event.target.closest("[data-labeler-task]");
    if (!button) return;
    void openBatch(button.dataset.labelerTask);
  });
  $("#labelerPanes")?.addEventListener("click", (event) => {
    const pageBtn = event.target.closest("[data-labeler-page-tab]");
    if (pageBtn) {
      setLabelerPage(pageBtn.dataset.labelerPageTab);
      return;
    }
    const btn = event.target.closest("[data-labeler-pane]");
    if (!btn) return;
    setLabelerPane(btn.dataset.labelerPane);
  });
  $("#labelerSkillSelect")?.addEventListener("change", () => { void applySelectedSkill(); });
  $("#labelerSkillClear")?.addEventListener("click", clearSystemPrompt);
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
  $("#labelerOpenExports")?.addEventListener("click", () => { void openExportFolder(); });
  $("#labelerWipeAnnotations")?.addEventListener("click", () => { void wipeAnnotations(); });

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
