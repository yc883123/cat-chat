// ============================================================
// 24-comfyui.js —— 造梦机（工作台第三个功能页）
// 只 import 01-core / 03-media / 22-studio；轮询经 onStudioPageChange 暂停/恢复
// （离开页面就停，回来由服务端状态校准，与打标器同一套约定）。
// 与聊天域零耦合：只调 /api/comfyui/*。
// ============================================================

import { $, api, localFileUrl, state, toast } from "./01-core.js";
import { attachmentThumbUrl, isPywebview, mediaKind, saveLocalFile } from "./03-media.js";
import { currentStudioPage, onStudioPageChange, setStudioPage } from "./22-studio.js";
const POLL_MS = 1200;
const REFRESH_MS = 5000;

const comfyui = {
  workflows: [],
  selectedId: "",
  detail: null,
  tasks: [],
  activeTaskId: "",
  eventSeq: 0,
  pollTimer: null,
  refreshTimer: null,
  unsubscribePage: null,
  importing: false,
  submitting: false,
  url: "",
  modelCandidates: null,   // 模型候选缓存 {checkpoints, loras}；null = 还没拉过
  deleting: new Set(),     // 正在「取消并删除」的任务 id，防重复提交
  mediaSelection: new Map(), // 媒体槽已选文件 {slotKey: {path, name, thumb_path}}；切工作流清空
  drafts: new Map(),       // 主表单用户已填的值 {工作流id|node_id|field_name: value}；重绘与刷新都保留，点「默认」才清
  uploadingSlot: "",       // 正在上传的媒体槽 key（防并发点选；批量拖拽时是各 key 拼接，仍为真值即锁住）
  mediaGroupsCollapsed: {}, // 媒体分组卡折叠状态 {kind: true}；只存内存，不落盘、不进 visible_params
  storyboardQueue: [],    // 创作场「分镜生图」发来的提示词；只存内存，刷新即清
};

// ============================================================
// 入口 / 生命周期
// ============================================================

export function bindComfyuiEvents() {
  if (comfyui.unsubscribePage) comfyui.unsubscribePage();
  comfyui.unsubscribePage = onStudioPageChange((page) => {
    if (page === "comfyui") {
      void enterPage();
    } else {
      stopPolling();
    }
  });

  $("#comfyuiTestConn")?.addEventListener("click", () => void refreshHealth());
  $("#comfyuiEditUrl")?.addEventListener("click", openUrlDialog);
  $("#comfyuiUrlCancel")?.addEventListener("click", closeUrlDialog);
  $("#comfyuiUrlSave")?.addEventListener("click", () => void saveUrl());
  $("#comfyuiRefreshWorkflows")?.addEventListener("click", () => void refreshWorkflows());
  $("#comfyuiImportBtn")?.addEventListener("click", openImportDialog);
  $("#comfyuiImportCancel")?.addEventListener("click", closeImportDialog);
  $("#comfyuiImportConfirm")?.addEventListener("click", () => void confirmImport());
  $("#comfyuiImportFile")?.addEventListener("change", onImportFile);
  $("#comfyuiSubmit")?.addEventListener("click", () => void submitTask());
  $("#comfyuiClearTasks")?.addEventListener("click", () => void clearFinishedTasks());
  $("#comfyuiFreeModels")?.addEventListener("click", () => void freeModels());
  $("#comfyuiOpenOutputs")?.addEventListener("click", () => void openOutputsFolder());
  $("#comfyuiParamsSave")?.addEventListener("click", () => void saveParamsConfig());
  $("#comfyuiParamsCancel")?.addEventListener("click", closeParamsDialog);
  $("#comfyuiParamsPicker")?.addEventListener("change", () => {
    const error = $("#comfyuiParamsError");
    if (error) error.textContent = "";
  });
  // 上下移按钮走 click 委托（行是每次打开重建的）；拖拽在 bindParamsPickerDrag 里。
  $("#comfyuiParamsPicker")?.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-move]");
    if (!button) return;
    event.preventDefault();
    movePickerRow(button.closest(".comfyui-params-item"), button.dataset.move);
  });
  bindParamsPickerDrag();
  $("#comfyuiWorkflowList")?.addEventListener("click", onWorkflowListClick);
  $("#comfyuiParamForm")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-comfyui-edit-params]")) openParamsDialog();
    else if (event.target.closest("[data-comfyui-reset-params]")) resetParamValues();
  });
  // 用户边填边记草稿：重绘（切页回来、传完媒体、保存显示集）时按草稿复原。
  for (const type of ["input", "change"]) {
    $("#comfyuiParamForm")?.addEventListener(type, (event) => {
      const input = event.target.closest?.("[data-comfyui-slot]");
      if (!input) return;
      rememberDraft(String(input.dataset.comfyuiSlot || ""), input.value);
    });
  }
  $("#comfyuiTaskList")?.addEventListener("click", onTaskListClick);
  $("#comfyuiOutputGrid")?.addEventListener("click", onOutputGridClick);
  $("#comfyuiPanes")?.addEventListener("click", onPaneClick);
  // 分镜队列：创作场跨页交接 + 逐条填入正向提示词。
  document.addEventListener("naiba:storyboard-handoff", onStoryboardHandoff);
  $("#comfyuiStoryboardClear")?.addEventListener("click", () => {
    comfyui.storyboardQueue = [];
    renderStoryboardQueue();
  });
  $("#comfyuiStoryboardList")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-comfyui-storyboard-fill]");
    if (!button) return;
    fillStoryboardPrompt(Number(button.dataset.comfyuiStoryboardFill));
  });
}

async function enterPage() {
  void refreshHealth();
  void refreshWorkflows();
  void refreshTasks();
  void loadModelCandidates();
  watchComfyuiWidth();
  renderParamForm();
  renderStoryboardQueue();
  scheduleBackgroundRefresh();
}

function onPaneClick(event) {
  const btn = event.target.closest("[data-comfyui-pane]");
  if (!btn) return;
  setComfyuiPane(btn.dataset.comfyuiPane);
}

// 三栏放不下时改回三档（工作流 / 提交 / 结果），中栏才拿得到整页宽度。
// 判档口径是**这一页自己的宽度**而不是视口：侧栏拖宽、文件面板打开都会让页面变窄
// 而视口不变（与顶栏 watchTopbarWidth 同一教训）。阈值来自实测：页面 1000px 以下
// 中栏就只剩 250px 上下——状态栏虽然换行兜住不再溢出，参数表单也已经没法用。
const NARROW_PAGE_WIDTH = 1000;
let comfyuiWidthObserver = null;

function watchComfyuiWidth() {
  const root = $("#studioPageComfyui");
  if (!root) return;
  const apply = (width) => {
    const narrow = width > 0 && width < NARROW_PAGE_WIDTH;
    if (narrow) root.dataset.comfyuiNarrow = "1";
    else delete root.dataset.comfyuiNarrow;
  };
  apply(root.getBoundingClientRect().width);
  if (typeof ResizeObserver !== "function") {
    window.addEventListener("resize", () => apply(root.getBoundingClientRect().width));
    return;
  }
  comfyuiWidthObserver?.disconnect();
  comfyuiWidthObserver = new ResizeObserver((entries) => {
    const box = entries[entries.length - 1]?.contentRect;
    apply(box ? Math.round(box.width) : root.getBoundingClientRect().width);
  });
  comfyuiWidthObserver.observe(root);
}

function narrowPanes() {
  return $("#studioPageComfyui")?.dataset.comfyuiNarrow === "1";
}

function setComfyuiPane(pane) {
  const root = $("#studioPageComfyui");
  if (!root) return;
  root.dataset.comfyuiPane = pane;
  document.querySelectorAll("#comfyuiPanes [data-comfyui-pane]").forEach((node) => {
    const active = node.dataset.comfyuiPane === pane;
    node.classList.toggle("active", active);
    node.setAttribute("aria-pressed", String(active));
  });
}

function scheduleBackgroundRefresh() {
  stopBackgroundRefresh();
  comfyui.refreshTimer = setInterval(() => {
    if (currentStudioPage() !== "comfyui") return;
    // 轻量保活：任务列表轮询走 pollActiveTask；这里只补工作流/连接的偶发变化。
    void refreshTasks(true);
  }, REFRESH_MS);
}

function stopBackgroundRefresh() {
  if (comfyui.refreshTimer) {
    clearInterval(comfyui.refreshTimer);
    comfyui.refreshTimer = null;
  }
}

// ============================================================
// 连接状态
// ============================================================

async function refreshHealth() {
  const dot = $("#comfyuiStatusDot");
  const text = $("#comfyuiStatusText");
  try {
    const payload = await api("/api/comfyui/health");
    if (payload.url) comfyui.url = String(payload.url);
    if (payload.ok) {
      dot?.classList.add("ok");
      dot?.classList.remove("error");
      const device = (payload.devices || []).map((item) => item.name).filter(Boolean)[0] || "";
      const version = payload.version ? ` · ${payload.version}` : "";
      if (text) text.textContent = `已连接 ${payload.url}${version}${device ? ` · ${device}` : ""}`;
    } else {
      markUnreachable(payload.error || "ComfyUI 未连接");
    }
  } catch (error) {
    markUnreachable(`ComfyUI 未连接：${error.message}`);
  }
}

// ---- 改地址 ----

function openUrlDialog() {
  const dialog = $("#comfyuiUrlDialog");
  if (!dialog) return;
  $("#comfyuiUrlInput").value = comfyui.url || "";
  $("#comfyuiUrlError").textContent = "";
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeUrlDialog() {
  const dialog = $("#comfyuiUrlDialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

async function saveUrl() {
  const raw = String($("#comfyuiUrlInput")?.value || "").trim();
  const error = $("#comfyuiUrlError");
  if (!raw) {
    if (error) error.textContent = "请填写 ComfyUI 地址，例如 http://127.0.0.1:8188。";
    return;
  }
  const button = $("#comfyuiUrlSave");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/comfyui/url", { method: "POST", body: { url: raw } });
    comfyui.url = String(payload.url || raw);
    closeUrlDialog();
    await refreshHealth();
    if (payload.reachable) toast(`地址已保存并连通：${payload.url}`);
    else toast(`地址已保存，但现在连不上 ComfyUI：${payload.error || "请确认它已启动"}`);
  } catch (caught) {
    if (error) error.textContent = String(caught.message || "保存失败");
  } finally {
    if (button) button.disabled = false;
  }
}

function markUnreachable(message) {
  const dot = $("#comfyuiStatusDot");
  const text = $("#comfyuiStatusText");
  dot?.classList.add("error");
  dot?.classList.remove("ok");
  if (text) text.textContent = message;
}

// ============================================================
// 工作流库
// ============================================================

async function refreshWorkflows() {
  try {
    const payload = await api("/api/comfyui/workflows");
    comfyui.workflows = payload.workflows || [];
    if (comfyui.selectedId && !comfyui.workflows.some((row) => row.id === comfyui.selectedId)) {
      comfyui.selectedId = "";
      comfyui.detail = null;
      renderParamForm();
    }
    renderWorkflowList();
  } catch (error) {
    toast(`读取工作流列表失败：${error.message}`);
  }
  // 「刷新」是用户在 ComfyUI 侧动过之后的显式动作：模型目录清单一并重拉，
  // 参数栏的模型/LoRA 下拉下一次展开就是新目录（下拉是展开时现读缓存）。
  void loadModelCandidates(true);
}

function renderWorkflowList() {
  const list = $("#comfyuiWorkflowList");
  if (!list) return;
  list.textContent = "";
  if (!comfyui.workflows.length) {
    const empty = document.createElement("p");
    empty.className = "comfyui-empty";
    empty.textContent = "还没有工作流。点「导入」把你在 ComfyUI 里跑通的工作流（API 格式 JSON）加进来。";
    list.append(empty);
    return;
  }
  for (const row of comfyui.workflows) {
    const card = document.createElement("div");
    card.className = "comfyui-workflow-card" + (row.id === comfyui.selectedId ? " active" : "");
    card.dataset.workflowId = row.id;
    const title = document.createElement("button");
    title.type = "button";
    title.className = "comfyui-workflow-title";
    title.textContent = row.name || "未命名工作流";
    const meta = document.createElement("span");
    meta.className = "comfyui-workflow-meta";
    meta.textContent = `${row.node_count} 个节点 · ${row.param_count} 个可调参数`;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "comfyui-workflow-delete";
    del.dataset.comfyuiDeleteWorkflow = row.id;
    del.title = "删除这份工作流";
    del.textContent = "删除";
    card.append(title, meta, del);
    list.append(card);
  }
}

async function onWorkflowListClick(event) {
  const del = event.target.closest("[data-comfyui-delete-workflow]");
  if (del) {
    const id = del.dataset.comfyuiDeleteWorkflow;
    const row = comfyui.workflows.find((item) => item.id === id);
    if (!window.confirm(`删除工作流「${row?.name || id}」？已生成的历史任务不受影响。`)) return;
    try {
      await api(`/api/comfyui/workflows/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (comfyui.selectedId === id) {
        comfyui.selectedId = "";
        comfyui.detail = null;
        renderParamForm();
      }
      await refreshWorkflows();
    } catch (error) {
      toast(`删除失败：${error.message}`);
    }
    return;
  }
  const title = event.target.closest(".comfyui-workflow-title");
  if (title) {
    const id = title.closest("[data-workflow-id]")?.dataset.workflowId || "";
    await selectWorkflow(id);
    if (narrowPanes()) setComfyuiPane("run");
  }
}

async function selectWorkflow(id) {
  comfyui.selectedId = id;
  comfyui.mediaSelection.clear();
  renderWorkflowList();
  try {
    const payload = await api(`/api/comfyui/workflows/${encodeURIComponent(id)}`);
    comfyui.detail = payload.workflow || null;
  } catch (error) {
    comfyui.detail = null;
    toast(`读取工作流详情失败：${error.message}`);
  }
  renderParamForm();
}

// 槽位渲染顺序：正向提示词 → 负向提示词 → 模型 → 媒体输入 → 其余（保持导入扫描顺序）。
const ROLE_ORDER = {
  positive_prompt: 0, negative_prompt: 1, checkpoint: 2, lora: 3,
  unet: 3, clip: 3, vae: 3, controlnet: 3, image: 4,
};
const ROLE_LABELS = {
  positive_prompt: "正向提示词",
  negative_prompt: "负向提示词",
  sampler: "采样器",
  scheduler: "调度器",
  checkpoint: "模型（Checkpoint）",
  lora: "LoRA 模型",
  unet: "模型（UNET）",
  clip: "模型（CLIP）",
  vae: "模型（VAE）",
  controlnet: "模型（ControlNet）",
  resolution: "分辨率",
};

// 媒体输入槽位（后端显式映射的加载节点）：field_type 即媒体类型。
const MEDIA_FIELD_TYPES = new Set(["image", "audio", "video"]);
const MEDIA_ACCEPT = {
  image: ".png,.jpg,.jpeg,.webp,.gif",
  audio: ".wav,.mp3,.m4a,.ogg,.flac",
  video: ".mp4,.webm,.mov,.m4v,.ogv",
};

function slotRank(slot) {
  if (ROLE_ORDER[slot.role] !== undefined) return ROLE_ORDER[slot.role];
  // 媒体输入排在无角色参数之前（计划 §4：媒体参数放在用户容易找到的位置）。
  if (MEDIA_FIELD_TYPES.has(slot.field_type)) return 4;
  return 5;
}

function orderedSlots(slots) {
  // 用户拖过顺序的工作流按保存的顺序排；没拖过的仍按角色优先级 + 导入顺序。
  // 保存过顺序之后新扫出来的槽（换了工作流版本）排在已排序的后面，不会被藏掉。
  const saved = savedOrderIndex();
  return slots
    .map((slot, index) => ({ slot, index }))
    .sort((a, b) => {
      if (saved.size) {
        const rankA = saved.get(slotKey(a.slot));
        const rankB = saved.get(slotKey(b.slot));
        if (rankA !== undefined || rankB !== undefined) {
          if (rankA === undefined) return 1;
          if (rankB === undefined) return -1;
          if (rankA !== rankB) return rankA - rankB;
          return a.index - b.index;
        }
      }
      const rankA = slotRank(a.slot);
      const rankB = slotRank(b.slot);
      return rankA !== rankB ? rankA - rankB : a.index - b.index;
    })
    .map((item) => item.slot);
}

function slotKey(slot) {
  return `${slot.node_id}|${slot.field_name}`;
}

// 「编辑参数」里拖出来的先后顺序（按工作流保存）：参数键 → 序号。
function savedOrderIndex() {
  const rows = comfyui.detail?.param_order;
  const map = new Map();
  if (!Array.isArray(rows)) return map;
  for (const item of rows) {
    if (!item) continue;
    map.set(`${item.node_id}|${item.field_name}`, map.size);
  }
  return map;
}

// 主表单默认显示的常用参数：提示词、种子、步数、采样器、调度器、模型、LoRA、分辨率。
// 其余已识别的参数留在数据里，用户可从「编辑参数」加进主表单。
const DEFAULT_VISIBLE_ROLES = new Set([
  "positive_prompt", "negative_prompt", "sampler", "scheduler", "checkpoint", "lora",
  "resolution",
]);

function defaultVisibleKeys(slots) {
  const set = new Set();
  for (const slot of slots) {
    if (DEFAULT_VISIBLE_ROLES.has(slot.role) || slot.field_type === "seed"
        || MEDIA_FIELD_TYPES.has(slot.field_type) || slot.field_name === "steps") {
      set.add(slotKey(slot));
    }
  }
  return set;
}

// 主表单可见参数：按工作流保存过就用保存的；没保存过用常用参数默认集。
// 媒体槽合并只对「媒体支持之前保存的旧显示集」生效：saveParamsConfig 保存成功后
// 会打 v2 标记（localStorage，按工作流 id），带标记的显示集按用户实际勾选执行，
// 「编辑参数」里取消勾选的媒体槽不再被强制合并回来（2026-10-26 计划 §4.5）。
const VISIBLE_MEDIA_V2_PREFIX = "comfyui.visibleMediaV2:";

function visibleSetKnowsMedia() {
  const id = String(comfyui.detail?.id || "");
  try {
    return localStorage.getItem(VISIBLE_MEDIA_V2_PREFIX + id) === "1";
  } catch (_) {
    return false;
  }
}

function markVisibleSetKnowsMedia() {
  const id = String(comfyui.detail?.id || "");
  try {
    localStorage.setItem(VISIBLE_MEDIA_V2_PREFIX + id, "1");
  } catch (_) { /* 隐私模式存不进：退回旧合并行为，不影响功能 */ }
}

function visibleKeySet() {
  const slots = comfyui.detail?.params || [];
  const stored = comfyui.detail?.visible_params;
  if (Array.isArray(stored)) {
    const set = new Set(stored.map((item) => `${item.node_id}|${item.field_name}`));
    if (!visibleSetKnowsMedia()) {
      // 媒体输入是后加能力：旧工作流保存过的显示集里不会有它们，合并进来保证
      // 「图片/音频/视频输入」始终可见。
      for (const slot of slots) {
        if (MEDIA_FIELD_TYPES.has(slot.field_type)) set.add(slotKey(slot));
      }
    }
    return set;
  }
  return defaultVisibleKeys(slots);
}

function renderParamForm() {
  const form = $("#comfyuiParamForm");
  if (!form) return;
  form.textContent = "";
  const submit = $("#comfyuiSubmit");
  if (submit) submit.disabled = !comfyui.detail;
  if (!comfyui.detail) {
    const empty = document.createElement("p");
    empty.className = "comfyui-empty";
    empty.textContent = "先在左侧选一份工作流，这里会列出它可以调的参数。";
    form.append(empty);
    return;
  }
  const head = document.createElement("div");
  head.className = "comfyui-param-heading-row";
  const heading = document.createElement("p");
  heading.className = "comfyui-param-heading";
  heading.textContent = comfyui.detail.name || "未命名工作流";
  const actions = document.createElement("span");
  actions.className = "comfyui-param-heading-actions";
  // 「默认」只把已填的内容还原成工作流里原本的值；「编辑」保存的是显示哪些参数、
  // 按什么顺序排，两者互不覆盖（用户口径 2026-10-29）。
  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "control-button tiny";
  reset.dataset.comfyuiResetParams = "1";
  reset.textContent = "默认";
  reset.title = "把填过的参数还原成工作流里原本的值；「编辑」保存的显示项与顺序不受影响";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "control-button tiny";
  edit.dataset.comfyuiEditParams = "1";
  edit.textContent = "编辑";
  edit.title = "选择要在主表单显示的参数，并调整它们的先后顺序";
  actions.append(reset, edit);
  head.append(heading, actions);
  form.append(head);
  const visible = visibleKeySet();
  const slots = orderedSlots(comfyui.detail.params || []).filter(
    (slot) => visible.has(slotKey(slot)));
  if (!slots.length) {
    const note = document.createElement("p");
    note.className = "comfyui-empty";
    note.textContent = "这份工作流没有可调的参数，直接点「开始生成」即可。";
    form.append(note);
  }
  // 媒体槽不逐槽渲染，按 field_type 归堆成「图片 / 音频 / 视频」分组卡（§4.1）：
  // orderedSlots() 里全部媒体槽同属 rank 4，卡片插在第一个媒体槽的位置一次性放好；
  // 非媒体槽照旧逐槽渲染，相对顺序与现在完全一致。归堆只认 field_type，
  // 与节点号、节点名、端口名解耦——工作流节点怎么命名都不影响分组。
  // 同一角色出现多个槽位（如两个 LoRA 加载器）时，完整标签本身带节点号与节点名，
  // 不再额外拼一次节点信息。
  const mediaGroups = new Map();
  const rowItems = [];
  let groupsPlaced = false;
  for (const slot of slots) {
    const kind = mediaKindOfSlot(slot);
    if (kind) {
      if (!groupsPlaced) {
        rowItems.push({ groups: true });
        groupsPlaced = true;
      }
      if (!mediaGroups.has(kind)) mediaGroups.set(kind, []);
      mediaGroups.get(kind).push(slot);
      continue;
    }
    rowItems.push({ slot });
  }
  for (const item of rowItems) {
    if (item.slot) {
      form.append(renderSlot(item.slot));
    } else {
      // 没有该类型节点就不生成该类型的卡（§3.1）。
      for (const kind of MEDIA_GROUP_ORDER) {
        const groupSlots = mediaGroups.get(kind) || [];
        if (groupSlots.length) form.append(renderMediaGroup(kind, groupSlots));
      }
    }
  }
}

// ---- 编辑参数 ----

function openParamsDialog() {
  if (!comfyui.detail) return;
  const dialog = $("#comfyuiParamsDialog");
  if (!dialog) return;
  renderParamsPicker();
  const error = $("#comfyuiParamsError");
  if (error) error.textContent = "";
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeParamsDialog() {
  const dialog = $("#comfyuiParamsDialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function renderParamsPicker() {
  const picker = $("#comfyuiParamsPicker");
  if (!picker) return;
  picker.textContent = "";
  const checked = visibleKeySet();
  for (const slot of orderedSlots(comfyui.detail?.params || [])) {
    const row = document.createElement("label");
    row.className = "comfyui-params-item";
    row.draggable = true;
    row.dataset.paramKey = slotKey(slot);
    const handle = document.createElement("span");
    handle.className = "comfyui-params-handle";
    handle.textContent = "⠿";
    handle.setAttribute("aria-hidden", "true");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.dataset.paramKey = slotKey(slot);
    box.checked = checked.has(slotKey(slot));
    const text = document.createElement("span");
    text.className = "comfyui-params-text";
    const roleLabel = ROLE_LABELS[slot.role];
    text.textContent = roleLabel ? `${roleLabel} · ${slot.label}` : slot.label;
    row.append(handle, box, text, moveButtons(row));
    picker.append(row);
  }
}

// 触屏和键盘没法拖：给每行配一对上下移按钮，落序与拖拽走同一份 DOM 顺序。
function moveButtons(row) {
  const wrap = document.createElement("span");
  wrap.className = "comfyui-params-move";
  for (const [dir, label, title] of [["up", "↑", "上移"], ["down", "↓", "下移"]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "comfyui-params-move-btn";
    button.dataset.move = dir;
    button.textContent = label;
    button.title = title;
    button.setAttribute("aria-label", title);
    wrap.append(button);
  }
  return wrap;
}

function movePickerRow(row, direction) {
  const picker = $("#comfyuiParamsPicker");
  if (!picker || !row) return;
  const sibling = direction === "up" ? row.previousElementSibling : row.nextElementSibling;
  if (!sibling) return;
  if (direction === "up") picker.insertBefore(row, sibling);
  else picker.insertBefore(sibling, row);
  clearParamsError();
  row.querySelector("input[type=checkbox]")?.focus();
}

function clearParamsError() {
  const error = $("#comfyuiParamsError");
  if (error) error.textContent = "";
}

// 「编辑参数」清单的拖拽落序：行是每次打开重建的，事件委托在容器上。
function bindParamsPickerDrag() {
  const picker = $("#comfyuiParamsPicker");
  if (!picker) return;
  let dragging = null;
  picker.addEventListener("dragstart", (event) => {
    const row = event.target.closest?.(".comfyui-params-item");
    if (!row) return;
    dragging = row;
    row.classList.add("is-dragging");
    event.dataTransfer.effectAllowed = "move";
    try { event.dataTransfer.setData("text/plain", row.dataset.paramKey || ""); } catch (_) { /* 老浏览器不认 */ }
  });
  picker.addEventListener("dragend", () => {
    picker.querySelectorAll(".is-dragging, .drop-target")
      .forEach((el) => el.classList.remove("is-dragging", "drop-target"));
    dragging = null;
  });
  picker.addEventListener("dragover", (event) => {
    if (!dragging) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const over = event.target.closest?.(".comfyui-params-item");
    picker.querySelectorAll(".drop-target").forEach((el) => el.classList.remove("drop-target"));
    if (!over || over === dragging) return;
    over.classList.add("drop-target");
  });
  picker.addEventListener("drop", (event) => {
    if (!dragging) return;
    event.preventDefault();
    const over = event.target.closest?.(".comfyui-params-item");
    picker.querySelectorAll(".drop-target").forEach((el) => el.classList.remove("drop-target"));
    if (!over || over === dragging) return;
    // 落在目标行的下半部分就插到它后面，和肉眼看到的插入位置一致。
    const rect = over.getBoundingClientRect();
    const after = event.clientY > rect.top + rect.height / 2;
    picker.insertBefore(dragging, after ? over.nextElementSibling : over);
    clearParamsError();
  });
}

async function saveParamsConfig() {
  if (!comfyui.detail) return;
  const picker = $("#comfyuiParamsPicker");
  if (!picker) return;
  const visible = [];
  const order = [];
  // DOM 顺序就是用户拖出来的顺序：勾选决定显示哪些，顺序决定主表单里谁在上面。
  for (const box of picker.querySelectorAll("input[type=checkbox]")) {
    if (!box.dataset.paramKey) continue;
    const [nodeId, ...rest] = String(box.dataset.paramKey).split("|");
    const row = { node_id: nodeId, field_name: rest.join("|") };
    order.push(row);
    if (box.checked) visible.push(row);
  }
  const button = $("#comfyuiParamsSave");
  if (button) button.disabled = true;
  try {
    const payload = await api(
      `/api/comfyui/workflows/${encodeURIComponent(comfyui.detail.id)}/params`,
      { method: "POST", body: { visible, order } });
    comfyui.detail = {
      ...comfyui.detail,
      visible_params: payload.visible_params || visible,
      param_order: payload.param_order || order,
    };
    // 从此这份工作流的显示集就是「知道媒体槽」的新格式：按实际勾选执行。
    markVisibleSetKnowsMedia();
    closeParamsDialog();
    renderParamForm();
    toast("参数显示与顺序已保存，这份工作流下次打开也照此显示");
  } catch (error) {
    const line = $("#comfyuiParamsError");
    if (line) line.textContent = String(error.message || "保存失败");
  } finally {
    if (button) button.disabled = false;
  }
}

// ---- 模型候选（Checkpoint / LoRA）----

async function loadModelCandidates(force = false) {
  if (!force && comfyui.modelCandidates !== null) return;
  try {
    const payload = await api("/api/comfyui/models");
    comfyui.modelCandidates = {
      checkpoints: Array.isArray(payload.models) ? payload.models : [],
      loras: Array.isArray(payload.loras) ? payload.loras : [],
      unets: Array.isArray(payload.unets) ? payload.unets : [],
      clips: Array.isArray(payload.clips) ? payload.clips : [],
      vaes: Array.isArray(payload.vaes) ? payload.vaes : [],
      controlnets: Array.isArray(payload.controlnets) ? payload.controlnets : [],
    };
  } catch (_) {
    // 候选拿不到就只留自由输入，不阻断编辑
    comfyui.modelCandidates = { checkpoints: [], loras: [], unets: [], clips: [], vaes: [], controlnets: [] };
  }
}

// 各模型加载角色对应的后端候选清单键；不在表里的角色没有离线兜底。
const ROLE_CANDIDATE_KEYS = {
  checkpoint: "checkpoints",
  lora: "loras",
  unet: "unets",
  clip: "clips",
  vae: "vaes",
  controlnet: "controlnets",
};

function modelCandidatesFor(role) {
  const cache = comfyui.modelCandidates;
  if (!cache) return [];
  const key = ROLE_CANDIDATE_KEYS[role];
  return key ? (cache[key] || []) : [];
}

// 槽位候选：节点声明里带来的枚举优先（schema 增强）；模型加载槽离线时回退
// /api/comfyui/models 的对应目录清单；都没有就自由输入。
function slotCandidates(slot) {
  const declared = Array.isArray(slot.candidates) ? slot.candidates : [];
  if (declared.length) return declared;
  if (ROLE_CANDIDATE_KEYS[slot.role]) return modelCandidatesFor(slot.role);
  return [];
}

// 给输入框挂可搜索候选下拉；候选不可用时静默，自由输入不受影响。
function attachCandidateCombo(input, wrap, candidatesFn) {
  const list = document.createElement("div");
  list.className = "comfyui-combo-list";
  list.hidden = true;
  const positionList = () => {
    if (list.hidden || !list.isConnected) return;
    const rect = input.getBoundingClientRect();
    const maxHeight = Math.min(190, window.innerHeight * 0.4);
    const below = window.innerHeight - rect.bottom - 8;
    const above = rect.top - 8;
    const openAbove = below < Math.min(maxHeight, 120) && above > below;
    const available = Math.max(80, Math.min(maxHeight, openAbove ? above : below));
    list.style.left = `${rect.left}px`;
    list.style.width = `${rect.width}px`;
    list.style.maxHeight = `${available}px`;
    list.style.top = openAbove
      ? `${Math.max(8, rect.top - available - 2)}px`
      : `${rect.bottom + 2}px`;
  };
  const trackPosition = () => {
    window.addEventListener("scroll", positionList, true);
    window.addEventListener("resize", positionList);
  };
  const stopTracking = () => {
    window.removeEventListener("scroll", positionList, true);
    window.removeEventListener("resize", positionList);
  };
  const showList = () => {
    list.hidden = false;
    if (!list.isConnected) {
      document.body.append(list);
      trackPosition();
    }
    positionList();
  };
  const hideList = () => {
    list.hidden = true;
    list.remove();
    stopTracking();
  };
  const renderMatches = (showAll = false) => {
    const text = String(input.value || "").trim().toLowerCase();
    const all = candidatesFn();
    const matched = showAll || !text
      ? all
      : all.filter((name) => name.toLowerCase().includes(text));
    const matches = matched.slice(0, 30);
    list.textContent = "";
    list.style.height = "auto";
    list.style.maxHeight = "";
    if (!matches.length) {
      const empty = document.createElement("div");
      empty.className = "comfyui-combo-option";
      empty.textContent = "没有匹配的候选；直接填文件名也可以";
      list.append(empty);
      showList();
      return;
    }
    for (const name of matches) {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "comfyui-combo-option";
      option.textContent = name;
      option.addEventListener("mousedown", (event) => {
        event.preventDefault();
        input.value = name;
        hideList();
      });
      list.append(option);
    }
    showList();
  };
  input.addEventListener("focus", () => renderMatches(true));
  input.addEventListener("input", () => renderMatches(false));
  input.addEventListener("blur", hideList);
}

// 主表单已填内容的来源：有草稿用草稿，否则用工作流里原本的值。
// 表单每次重绘（切页、传完媒体、改完「编辑」）都会重建控件，只认草稿才不会把
// 用户刚填的提示词打回工作流默认值（用户口径 2026-10-29）。
// 草稿键带工作流 id：不同工作流常有同名节点端口（如 3|seed），不隔离会互相串值。
function draftKey(key) {
  return `${comfyui.detail?.id || ""}|${key}`;
}

function savedDraft(key) {
  return comfyui.drafts.get(draftKey(key));
}

function initialFieldValue(slot, key) {
  const draft = savedDraft(key);
  return draft !== undefined ? draft : (slot.default ?? "");
}

// 用户改一个槽就记一条：重绘后仍按这里复原，刷新触发再也不会把提示词打回原值。
function rememberDraft(key, value) {
  comfyui.drafts.set(draftKey(key), String(value ?? ""));
}

// 「默认」：只把参数值还原成工作流原本的值，清空这份工作流的草稿后重绘；
// 「编辑」保存的显示项与顺序在 comfyui.detail 里，不受这条路径影响。
function resetParamValues() {
  if (!comfyui.detail) return;
  const prefix = `${comfyui.detail.id || ""}|`;
  for (const key of [...comfyui.drafts.keys()]) {
    if (key.startsWith(prefix)) comfyui.drafts.delete(key);
  }
  renderParamForm();
  toast("已还原成工作流里原本的参数值");
}

function renderSlot(slot) {
  const type = slot.field_type;
  // 媒体槽内部有自己的「选择文件」label：<label> 不能嵌 <label>，外层用 div。
  const isMedia = slot.control === "media" || MEDIA_FIELD_TYPES.has(type);
  const wrap = document.createElement(isMedia ? "div" : "label");
  wrap.className = "comfyui-param" + (isMedia ? " comfyui-param-media" : "");
  const title = document.createElement("span");
  title.className = "comfyui-param-label";
  // 宽/高两个槽位同属「分辨率」角色：主标签要分得清，否则表单里两行都叫「分辨率」。
  const roleLabel = slot.role === "resolution"
    ? (slot.field_name === "height" ? "分辨率（高）" : "分辨率（宽）")
    : ROLE_LABELS[slot.role];
  const fullLabel = slot.label || `${slot.node_id} · ${slot.field_name}`;
  // 有用途角色的槽位也要显示完整来源与端口映射；角色名只做前缀，不覆盖映射。
  title.textContent = roleLabel ? `${roleLabel} · ${fullLabel}` : fullLabel;
  title.title = fullLabel;
  wrap.append(title);
  const key = `${slot.node_id}|${slot.field_name}`;
  wrap.dataset.paramKey = key;
  if (isMedia) {
    wrap.append(renderMediaPicker(slot, key));
    return wrap;
  }
  if (type === "bool") {
    const select = document.createElement("select");
    select.dataset.comfyuiSlot = key;
    for (const [value, text] of [["true", "开"], ["false", "关"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      select.append(option);
    }
    const draft = savedDraft(key);
    select.value = draft === "true" || draft === "false"
      ? draft
      : (slot.default ? "true" : "false");
    wrap.append(select);
    return wrap;
  }
  if (slot.role === "positive_prompt" || slot.role === "negative_prompt") {
    // 主提示词是多行文本：单行 input 没法正常编辑长提示词；正向框给足高度，
    // 用户还能拖动下缘继续调。其他有角色的槽位（采样器/调度器/模型）走单行。
    const area = document.createElement("textarea");
    area.rows = slot.role === "positive_prompt" ? 9 : 5;
    area.dataset.comfyuiSlot = key;
    area.value = initialFieldValue(slot, key);
    area.spellcheck = false;
    wrap.append(area);
    return wrap;
  }
  if (slot.control === "multiline") {
    // 节点声明为多行字符串（如自定义多行文本节点）：同样给多行框。
    const area = document.createElement("textarea");
    area.rows = 4;
    area.dataset.comfyuiSlot = key;
    area.value = initialFieldValue(slot, key);
    area.spellcheck = false;
    wrap.append(area);
    return wrap;
  }
  if (type === "image") {
    // 声明拿不到时的兜底：老数据里的 image 槽按普通字符串处理。
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.comfyuiSlot = key;
    input.value = initialFieldValue(slot, key);
    input.placeholder = "已在 ComfyUI 输入目录里的图片文件名";
    wrap.append(input);
    return wrap;
  }
  const input = document.createElement("input");
  input.type = type === "number" ? "number" : "text";
  input.step = "any";
  input.dataset.comfyuiSlot = key;
  if (type === "seed") {
    input.placeholder = "留空 = 每次随机；填数字 = 固定种子";
    input.value = savedDraft(key) ?? "";
  } else {
    input.value = initialFieldValue(slot, key);
  }
  const candidates = slotCandidates(slot);
  if (slot.control === "combo" || ROLE_CANDIDATE_KEYS[slot.role]) {
    // 枚举/模型槽：可搜索下拉；候选不可用或当前值不在候选里也照常编辑提交，
    // 不静默替换（计划 §4）。
    input.autocomplete = "off";
    input.spellcheck = false;
    if (slot.role === "lora") {
      input.placeholder = "从 ComfyUI LoRA 里选，或直接输入文件名";
    } else if (ROLE_CANDIDATE_KEYS[slot.role]) {
      input.placeholder = "从 ComfyUI 模型里选，或直接输入文件名";
    }
    attachCandidateCombo(input, wrap, () => slotCandidates(slot));
  }
  wrap.append(input);
  return wrap;
}

// ---- 媒体输入槽（图片/音频/视频）：按类型分组卡片 + 槽位块 + 拖拽/点选上传 ----

const MEDIA_KIND_LABELS = { image: "图片", audio: "音频", video: "视频" };
// 三张卡的先后顺序：图片 → 音频 → 视频（沿用 ROLE_ORDER 的媒体顺序）。
const MEDIA_GROUP_ORDER = ["image", "audio", "video"];
// MEDIA_ACCEPT 是 ".png,.jpg" 形态；拖拽校验要的是扩展名集合。
const MEDIA_EXT_SETS = {};
for (const [kind, accept] of Object.entries(MEDIA_ACCEPT)) {
  MEDIA_EXT_SETS[kind] = new Set(accept.split(",").map((item) => item.trim().toLowerCase()));
}

const MEDIA_GROUP_ICONS = {
  image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>',
  audio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
  video: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="2" y="4" width="14" height="16" rx="2"/><path d="M16 10l6-3v10l-6-3z"/></svg>',
};
const MEDIA_PLAY_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';

// 判定口径与 renderSlot 的 isMedia 完全一致：control 声明或 field_type 命中即媒体槽；
// kind 取 slot.media（后端显式映射）优先。类型不在三集合里的边缘数据返回 ""，
// 继续走 renderSlot → renderMediaPicker 的老行渲染，不至于丢失控件。
function mediaKindOfSlot(slot) {
  if (slot.control !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type)) return "";
  const kind = slot.media || slot.field_type;
  return MEDIA_GROUP_ORDER.includes(kind) ? kind : "";
}

// 四态口径唯一实现（分组块与兜底行共用）：判定与文案一字不改（§3.2）。
// full 用于块 title/兜底行的整句；badge 是块上角标的短文案；
// clearTitle 是已选块「清除」按钮的三种说明（与旧版一致）。
function mediaSlotStatus(slot, picked) {
  const emptyInWorkflow = !String(slot.default ?? "").trim() || String(slot.default).trim() === "None";
  const skipSafe = slot.media_optional === true;
  if (picked) {
    return {
      badge: "已选",
      cls: "ok",
      full: `已选：${picked.name}`,
      clearTitle: skipSafe
        ? "清除选择：生成时跳过这个输入"
        : emptyInWorkflow
          ? "清除选择：回到工作流里的空值"
          : "清除选择：生成时按工作流原本的文件执行",
    };
  }
  if (skipSafe) return { badge: "未选即跳过", cls: "", full: "未选即跳过" };
  if (emptyInWorkflow) return { badge: "请选择文件", cls: "", full: "请选择文件" };
  return { badge: "沿用原文件", cls: "keep", full: "未选沿用原文件" };
}

// 块底端口小字：节点号 · 节点名 → 目标端口（截断省略；完整映射在块 title 与「编辑参数」里）。
function mediaPortText(slot) {
  const ports = Array.isArray(slot.connections)
    ? slot.connections.map((item) => String(item?.target_field || "")).filter(Boolean)
    : [];
  const target = ports.length ? ports.join("、") : String(slot.field_name || "");
  const head = String(slot.node_title || slot.class_type || "");
  const base = head ? `${slot.node_id} · ${head}` : String(slot.node_id);
  return target ? `${base} → ${target}` : base;
}

function renderMediaGroup(kind, slots) {
  const group = document.createElement("section");
  group.className = "comfyui-media-group";
  group.dataset.comfyuiMediaGroup = kind;
  const collapsed = comfyui.mediaGroupsCollapsed[kind] === true;
  if (collapsed) group.classList.add("collapsed");
  const pickedCount = slots.filter(
    (slot) => comfyui.mediaSelection.get(slotKey(slot))?.path).length;
  const head = document.createElement("button");
  head.type = "button";
  head.className = "comfyui-media-group-head";
  head.setAttribute("aria-expanded", String(!collapsed));
  const icon = document.createElement("span");
  icon.className = "g-icon";
  icon.innerHTML = MEDIA_GROUP_ICONS[kind];
  const name = document.createElement("span");
  name.className = "g-name";
  name.textContent = MEDIA_KIND_LABELS[kind] || "媒体";
  const count = document.createElement("span");
  count.className = "g-count";
  count.textContent = `${slots.length} 个输入`;
  // 0/N 灰、部分选橙、全选绿：一眼看出这组还差几个（§3.1）。
  const fill = document.createElement("span");
  fill.className = "g-fill"
    + (pickedCount === 0 ? " idle" : pickedCount < slots.length ? " warn" : " ok");
  fill.textContent = `${pickedCount}/${slots.length} 已选`;
  const caret = document.createElement("span");
  caret.className = "caret";
  caret.textContent = "▾";
  head.append(icon, name, count, fill, caret);
  head.addEventListener("click", () => {
    comfyui.mediaGroupsCollapsed[kind] = !collapsed;
    rerenderParamForm();
  });
  const tiles = document.createElement("div");
  tiles.className = "comfyui-media-tiles";
  for (const slot of slots) tiles.append(renderMediaTile(slot, slotKey(slot), kind));
  const hint = document.createElement("p");
  hint.className = "comfyui-media-drop-hint";
  hint.textContent = "把文件拖到卡片任意处，会按槽位顺序依次填进空位";
  group.append(head, tiles, hint);
  wireMediaGroupDrop(group, tiles, slots, kind);
  return group;
}

function renderMediaTile(slot, key, kind) {
  const picked = comfyui.mediaSelection.get(key) || null;
  const status = mediaSlotStatus(slot, picked);
  // 「沿用原文件」的槽按实块渲染（占位图标 + 原文件名），不是虚线空块（§3.4）。
  const keepOriginal = !picked && status.cls === "keep";
  const tile = document.createElement("div");
  tile.className = "comfyui-media-tile"
    + (kind === "audio" ? " audio" : kind === "video" ? " video" : "");
  if (!picked && !keepOriginal) tile.classList.add("empty");
  tile.dataset.comfyuiMediaTile = key;
  tile.title = `${slot.label || ""}\n${status.full}`;
  const file = document.createElement("input");
  file.type = "file";
  file.accept = MEDIA_ACCEPT[kind] || "";
  file.hidden = true;
  file.addEventListener("change", () => void onMediaPicked(slot, key, file));

  const badge = document.createElement("span");
  badge.className = "t-status" + (status.cls ? ` ${status.cls}` : "");
  badge.textContent = status.badge;

  if (picked || keepOriginal) {
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    const actions = document.createElement("div");
    actions.className = "t-actions";
    const replace = document.createElement("button");
    replace.type = "button";
    replace.textContent = "⇄";
    replace.title = picked ? "替换这个文件" : `选择文件替换（当前沿用 ${slot.default}）`;
    replace.setAttribute("aria-label", replace.title);
    replace.addEventListener("click", (event) => {
      event.stopPropagation();
      file.click();
    });
    actions.append(replace);
    if (picked) {
      const clear = document.createElement("button");
      clear.type = "button";
      clear.textContent = "✕";
      clear.title = status.clearTitle;
      clear.setAttribute("aria-label", clear.title);
      clear.addEventListener("click", (event) => {
        event.stopPropagation();
        comfyui.mediaSelection.delete(key);
        rerenderParamForm();
      });
      actions.append(clear);
    }
    if (picked) {
      const record = { path: picked.path, thumb_path: picked.thumb_path || "", name: picked.name || "" };
      if (kind === "image") {
        const img = document.createElement("img");
        img.src = attachmentThumbUrl(record);
        img.alt = record.name;
        img.loading = "lazy";
        // 放大走 03-media 的 document 级灯箱委托（与产物卡同一入口）。
        img.dataset.largeUrl = localFileUrl(record.path);
        thumb.append(img);
      } else if (kind === "video") {
        // 块只有 ~96px 宽，内嵌控件没法用：preload=metadata 的首帧当海报 +
        // 播放按钮，点块走放大播放浮层（§4.2）。
        const video = document.createElement("video");
        video.src = localFileUrl(record.path);
        video.preload = "metadata";
        video.muted = true;
        video.playsInline = true;
        thumb.append(video);
        const play = document.createElement("span");
        play.className = "play";
        play.innerHTML = MEDIA_PLAY_ICON;
        thumb.append(play);
        const duration = document.createElement("span");
        duration.className = "t-duration";
        video.addEventListener("loadedmetadata", () => {
          duration.textContent = formatMediaDuration(video.duration);
        });
        thumb.append(duration);
        tile.addEventListener("click", (event) => {
          if (event.target.closest(".t-actions")) return;
          openMediaPreviewOverlay(localFileUrl(record.path), "video");
        });
      } else {
        const icon = document.createElement("span");
        icon.className = "ph-icon";
        icon.innerHTML = MEDIA_PLAY_ICON;
        thumb.append(icon);
        tile.addEventListener("click", (event) => {
          if (event.target.closest(".t-actions")) return;
          openMediaPreviewOverlay(localFileUrl(record.path), "audio");
        });
      }
    } else {
      const icon = document.createElement("span");
      icon.className = "ph-icon";
      icon.innerHTML = MEDIA_GROUP_ICONS[kind];
      thumb.append(icon);
      tile.addEventListener("click", (event) => {
        if (event.target.closest(".t-actions")) return;
        file.click();
      });
    }
    const name = document.createElement("div");
    name.className = "t-name";
    name.textContent = picked ? picked.name : String(slot.default ?? "");
    name.title = name.textContent;
    const port = document.createElement("div");
    port.className = "t-port";
    port.textContent = mediaPortText(slot);
    tile.append(thumb, badge, actions, name, port);
  } else {
    // 空块：虚线格，点它即唤起系统选择器；音频是扁平小条，端口并进标签行。
    const plus = document.createElement("span");
    plus.className = "plus";
    plus.textContent = "＋";
    const label = document.createElement("span");
    label.className = "plus-label";
    label.textContent = kind === "audio"
      ? `选择${MEDIA_KIND_LABELS[kind]} · ${mediaPortText(slot)}`
      : `选择${MEDIA_KIND_LABELS[kind] || "文件"}`;
    const dropLabel = document.createElement("span");
    dropLabel.className = "drop-label";
    dropLabel.textContent = "松开即上传";
    const wrap = document.createElement("div");
    wrap.className = "plus-wrap";
    wrap.append(plus, label, dropLabel);
    tile.append(badge, wrap);
    if (kind !== "audio") {
      const port = document.createElement("div");
      port.className = "t-port";
      port.textContent = mediaPortText(slot);
      tile.append(port);
    }
    tile.addEventListener("click", () => file.click());
  }
  tile.append(file);
  wireMediaTileDrop(tile, key, kind);
  return tile;
}

function formatMediaDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

// 视频/音频放大播放浮层：动态建节点（本模块不改 index.html），点遮罩或 Esc 关闭。
function openMediaPreviewOverlay(url, kind) {
  document.querySelector(".comfyui-media-preview-overlay")?.remove();
  const overlay = document.createElement("div");
  overlay.className = "comfyui-media-preview-overlay";
  const media = document.createElement(kind === "video" ? "video" : "audio");
  media.src = url;
  media.controls = true;
  media.autoplay = true;
  if (kind === "video") media.playsInline = true;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "comfyui-media-preview-close";
  close.textContent = "✕";
  close.title = "关闭";
  close.setAttribute("aria-label", "关闭预览");
  const shutdown = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (event) => {
    if (event.key === "Escape") shutdown();
  };
  close.addEventListener("click", shutdown);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) shutdown();
  });
  document.addEventListener("keydown", onKey);
  overlay.append(media, close);
  document.body.append(overlay);
}

// ---- 拖拽（桌面增强；触屏不派发 HTML5 drag 事件，点选才是主路径）----

function dragHasFiles(event) {
  return Boolean(event.dataTransfer?.types?.includes("Files"));
}

// 目录拖放一律拒绝：媒体槽只收单文件，不进文件夹索引那套链路（§4.3）。
function dropHasDirectory(event) {
  for (const item of event.dataTransfer?.items || []) {
    const entry = typeof item.webkitGetAsEntry === "function" ? item.webkitGetAsEntry() : null;
    if (entry?.isDirectory) return true;
  }
  return false;
}

function fileMatchesMediaKind(file, kind) {
  const name = String(file?.name || "").toLowerCase();
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot) : "";
  return (MEDIA_EXT_SETS[kind] || new Set()).has(ext);
}

function fileKindOf(file) {
  for (const kind of MEDIA_GROUP_ORDER) {
    if (fileMatchesMediaKind(file, kind)) return kind;
  }
  return "";
}

// 类型不符的统一提示：指明拖的是什么、该去哪张卡（§3.5）——目标卡是文件自己的类型卡，
// 不是当前拖到的这张（音频拖到图片卡要说「请拖到音频卡」）。
function toastMediaKindMismatch(file, kind) {
  const actual = fileKindOf(file);
  toast(actual
    ? `这是${MEDIA_KIND_LABELS[actual]}，请拖到${MEDIA_KIND_LABELS[actual]}卡`
    : `这个文件不是${MEDIA_KIND_LABELS[kind]}，请换${MEDIA_KIND_LABELS[kind]}文件`);
}

function wireMediaTileDrop(tile, key, kind) {
  tile.addEventListener("dragover", (event) => {
    if (!dragHasFiles(event)) return;
    // dragover 也要 preventDefault：不声明「这里可以放」，drop 根本不会派发。
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    tile.classList.add("dragover");
  });
  tile.addEventListener("dragleave", (event) => {
    if (event.relatedTarget && tile.contains(event.relatedTarget)) return;
    tile.classList.remove("dragover");
  });
  tile.addEventListener("drop", (event) => {
    if (!dragHasFiles(event)) return;
    event.preventDefault();
    // 块自己收下，不再冒泡给卡片按空位填。
    event.stopPropagation();
    tile.classList.remove("dragover");
    void handleTileMediaDrop(event, key, kind);
  });
}

async function handleTileMediaDrop(event, key, kind) {
  const files = Array.from(event.dataTransfer?.files || []);
  if (!files.length) return;
  if (dropHasDirectory(event)) {
    toast("媒体输入只收单个文件，不支持拖文件夹");
    return;
  }
  const bad = files.find((file) => !fileMatchesMediaKind(file, kind));
  if (bad) {
    toastMediaKindMismatch(bad, kind);
    return;
  }
  if (files.length > 1) {
    toast(`一次拖了 ${files.length} 个文件，只有第一个进入这个输入；要批量请拖到卡片空白处`);
  }
  await uploadMediaFiles([{ key, file: files[0] }]);
}

function wireMediaGroupDrop(group, tiles, slots, kind) {
  const setCardDrop = (on) => {
    tiles.classList.toggle("card-drop", on);
    group.classList.toggle("card-drop", on);
  };
  group.addEventListener("dragover", (event) => {
    if (!dragHasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setCardDrop(true);
  });
  group.addEventListener("dragleave", (event) => {
    if (event.relatedTarget && group.contains(event.relatedTarget)) return;
    setCardDrop(false);
  });
  group.addEventListener("drop", (event) => {
    if (!dragHasFiles(event)) return;
    event.preventDefault();
    setCardDrop(false);
    void handleGroupMediaDrop(event, slots, kind);
  });
}

async function handleGroupMediaDrop(event, slots, kind) {
  const files = Array.from(event.dataTransfer?.files || []);
  if (!files.length) return;
  if (dropHasDirectory(event)) {
    toast("媒体输入只收单个文件，不支持拖文件夹");
    return;
  }
  const bad = files.find((file) => !fileMatchesMediaKind(file, kind));
  if (bad) {
    toastMediaKindMismatch(bad, kind);
    return;
  }
  // 「沿用原文件」但本次没选的槽也算空位：拖进来的文件优先用上。
  const empties = slots.filter((slot) => !comfyui.mediaSelection.get(slotKey(slot))?.path);
  if (!empties.length) {
    toast("这张卡的输入都已选好，请拖到具体块上替换");
    return;
  }
  const assignCount = Math.min(files.length, empties.length);
  if (files.length > empties.length) {
    toast(`空位不够：先填 ${empties.length} 个空位，多出的 ${files.length - empties.length} 个请拖到具体块上替换`);
  }
  await uploadMediaFiles(files.slice(0, assignCount).map((file, index) => ({
    key: slotKey(empties[index]),
    file,
  })));
}

// 串行上传 + 单槽锁：批量拖拽逐个 await，全部完成后只重绘一次（§4.3），
// 避免逐槽重绘造成闪烁。失败逐个 toast，成功的照常写入 mediaSelection。
async function uploadMediaFiles(items) {
  if (comfyui.uploadingSlot) {
    toast("上一个文件还在上传，请稍候");
    return 0;
  }
  comfyui.uploadingSlot = items.map((item) => item.key).join("|");
  let okCount = 0;
  try {
    for (const { key, file } of items) {
      const form = new FormData();
      form.append("file", file, file.name);
      try {
        const payload = await api("/api/comfyui/media", { method: "POST", body: form });
        comfyui.mediaSelection.set(key, {
          path: String(payload.path || ""),
          name: String(payload.name || file.name),
          thumb_path: String(payload.thumb_path || ""),
        });
        okCount += 1;
      } catch (error) {
        toast(`上传失败：${error.message}`);
      }
    }
  } finally {
    comfyui.uploadingSlot = "";
    rerenderParamForm();
  }
  return okCount;
}

async function onMediaPicked(slot, key, fileInput) {
  const file = fileInput.files?.[0];
  fileInput.value = "";
  if (!file) return;
  await uploadMediaFiles([{ key, file }]);
}

// 兜底渲染：control=media 但类型不在图片/音频/视频里（手改存档等边缘数据）仍按
// 老的三件套行渲染，不至于丢控件；正常媒体槽都走 renderMediaGroup/renderMediaTile。
function renderMediaPicker(slot, key) {
  const kind = slot.media || slot.field_type;
  const wrap = document.createElement("div");
  wrap.className = "comfyui-media";
  wrap.dataset.comfyuiMediaSlot = key;
  const picked = comfyui.mediaSelection.get(key) || null;
  const preview = document.createElement("div");
  preview.className = "comfyui-media-preview";
  if (picked) {
    const record = { path: picked.path, thumb_path: picked.thumb_path || "", name: picked.name || "" };
    if (mediaKind(record.path, record.name) === "image") {
      const img = document.createElement("img");
      img.src = attachmentThumbUrl(record);
      img.alt = record.name;
      img.loading = "lazy";
      preview.append(img);
    } else if (mediaKind(record.path, record.name) === "video") {
      const video = document.createElement("video");
      video.src = localFileUrl(record.path);
      video.controls = true;
      video.preload = "metadata";
      preview.append(video);
    } else {
      const audio = document.createElement("audio");
      audio.src = localFileUrl(record.path);
      audio.controls = true;
      audio.preload = "metadata";
      preview.append(audio);
    }
  }
  const note = document.createElement("span");
  note.className = "comfyui-media-note";
  const status = mediaSlotStatus(slot, picked);
  note.textContent = status.full;
  note.title = status.full;
  const actions = document.createElement("div");
  actions.className = "comfyui-media-actions";
  const pickLabel = document.createElement("label");
  pickLabel.className = "control-button tiny comfyui-media-pick";
  pickLabel.textContent = picked ? "替换" : `选择${MEDIA_KIND_LABELS[kind] || "文件"}`;
  const file = document.createElement("input");
  file.type = "file";
  file.accept = MEDIA_ACCEPT[kind] || "";
  file.hidden = true;
  file.addEventListener("change", () => void onMediaPicked(slot, key, file));
  pickLabel.append(file);
  actions.append(pickLabel);
  if (picked) {
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "control-button tiny";
    clear.textContent = "清除";
    clear.title = status.clearTitle;
    clear.addEventListener("click", () => {
      comfyui.mediaSelection.delete(key);
      rerenderParamForm();
    });
    actions.append(clear);
  }
  wrap.append(preview, note, actions);
  return wrap;
}

function rerenderParamForm() {
  // 只重画表单：不动 comfyui.detail / mediaSelection（选择状态由调用方维护）。
  renderParamForm();
}

// ---- 导入 ----

function openImportDialog() {
  const dialog = $("#comfyuiImportDialog");
  if (!dialog) return;
  $("#comfyuiImportName").value = "";
  $("#comfyuiImportText").value = "";
  $("#comfyuiImportFile").value = "";
  $("#comfyuiImportError").textContent = "";
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeImportDialog() {
  const dialog = $("#comfyuiImportDialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function onImportFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  file.text().then((text) => {
    const paste = $("#comfyuiImportText");
    if (paste && !paste.value.trim()) paste.value = text;
    const name = $("#comfyuiImportName");
    if (name && !name.value.trim()) name.value = file.name.replace(/\.json$/i, "");
  });
}

async function confirmImport() {
  if (comfyui.importing) return;
  const raw = String($("#comfyuiImportText")?.value || "").trim();
  const name = String($("#comfyuiImportName")?.value || "").trim();
  const error = $("#comfyuiImportError");
  if (!raw) {
    if (error) error.textContent = "请先选择文件或粘贴工作流 JSON 内容。";
    return;
  }
  comfyui.importing = true;
  const button = $("#comfyuiImportConfirm");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/comfyui/workflows", {
      method: "POST",
      body: { name, workflow: raw },
    });
    closeImportDialog();
    await refreshWorkflows();
    await selectWorkflow(String(payload.workflow?.id || ""));
    toast(`已导入「${payload.workflow?.name || name}」，共 ${payload.workflow?.params?.length || 0} 个可调参数`);
  } catch (caught) {
    if (error) error.textContent = String(caught.message || "导入失败");
  } finally {
    comfyui.importing = false;
    if (button) button.disabled = false;
  }
}

// ============================================================
// 任务：提交 / 轮询 / 取消 / 重试 / 产物
// ============================================================

function collectOverrides() {
  const overrides = [];
  for (const slot of comfyui.detail?.params || []) {
    const key = `${slot.node_id}|${slot.field_name}`;
    // 只收主表单里渲染出来的输入：隐藏参数没有输入框，自然不会提交覆盖值，
    // 生成时按工作流里原本的值执行。
    const input = document.querySelector(`[data-comfyui-slot="${CSS.escape(key)}"]`);
    if (!input) continue;
    const text = String(input.value ?? "").trim();
    if (slot.field_type === "seed") {
      if (text) overrides.push({ node_id: slot.node_id, field_name: slot.field_name, value: text });
      continue;
    }
    if (slot.field_type === "bool") {
      overrides.push({ node_id: slot.node_id, field_name: slot.field_name, value: text === "true" });
      continue;
    }
    overrides.push({ node_id: slot.node_id, field_name: slot.field_name, value: text });
  }
  return overrides;
}

// 媒体槽已选的受管文件（提交时随任务走受管引用，执行前由服务端转传 ComfyUI）。
// 未选的槽不提交：声明为可选的连接由执行端移除，其余按工作流原本的值执行。
function collectMediaInputs() {
  const items = [];
  for (const slot of comfyui.detail?.params || []) {
    if ((slot.control || "") !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type)) continue;
    const picked = comfyui.mediaSelection.get(slotKey(slot));
    if (picked?.path) {
      items.push({ node_id: slot.node_id, field_name: slot.field_name, path: picked.path });
    }
  }
  return items;
}

async function submitTask() {
  if (comfyui.submitting || !comfyui.detail) return;
  const shotsInput = $("#comfyuiShots");
  let shots = 1;
  try {
    shots = Math.max(1, Math.min(200, Math.floor(Number(shotsInput?.value || 1))));
  } catch (_) { /* 回落默认 */ }
  comfyui.submitting = true;
  const button = $("#comfyuiSubmit");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/comfyui/tasks", {
      method: "POST",
      body: {
        workflow_id: comfyui.detail.id,
        overrides: collectOverrides(),
        media_inputs: collectMediaInputs(),
        shots,
      },
    });
    comfyui.activeTaskId = String(payload.task?.id || "");
    comfyui.eventSeq = 0;
    comfyui.tasks = [];
    renderTasks();
    await refreshTasks();
    startPolling();
    if (narrowPanes()) setComfyuiPane("tasks");
    toast("已提交，正在排队执行");
  } catch (error) {
    toast(`提交失败：${error.message}`);
  } finally {
    comfyui.submitting = false;
    if (button) button.disabled = !comfyui.detail;
  }
}

async function refreshTasks(silent = false) {
  try {
    const payload = await api("/api/comfyui/tasks");
    const previous = comfyui.tasks.find((row) => row.id === comfyui.activeTaskId);
    comfyui.tasks = payload.tasks || [];
    const active = comfyui.tasks.find((row) => isTaskActive(row));
    if (active) {
      comfyui.activeTaskId = String(active.id);
      if (String(previous?.status || "") !== String(active.status)) comfyui.eventSeq = 0;
      startPolling();
    } else if (!isTaskActiveById(comfyui.activeTaskId)) {
      stopPolling();
    }
    renderTasks();
    await renderLatestOutputs();
  } catch (error) {
    if (!silent) toast(`读取任务列表失败：${error.message}`);
  }
}

function isTaskActive(row) {
  return row.status === "queued" || row.status === "running";
}

function isTaskActiveById(id) {
  if (!id) return false;
  const row = comfyui.tasks.find((item) => item.id === id);
  return row ? isTaskActive(row) : false;
}

function startPolling() {
  if (comfyui.pollTimer) return;
  if (currentStudioPage() !== "comfyui") return;
  comfyui.pollTimer = setInterval(() => { void pollActiveTask(); }, POLL_MS);
}

function stopPolling() {
  if (comfyui.pollTimer) {
    clearInterval(comfyui.pollTimer);
    comfyui.pollTimer = null;
  }
}

async function pollActiveTask() {
  if (currentStudioPage() !== "comfyui") {
    stopPolling();
    return;
  }
  if (!isTaskActiveById(comfyui.activeTaskId)) {
    stopPolling();
    return;
  }
  try {
    const payload = await api(
      `/api/comfyui/tasks/${encodeURIComponent(comfyui.activeTaskId)}/events?after=${comfyui.eventSeq}`);
    const events = payload.events || [];
    if (events.length) comfyui.eventSeq = Number(payload.last_seq || comfyui.eventSeq);
    const taskPayload = await api(`/api/comfyui/tasks/${encodeURIComponent(comfyui.activeTaskId)}`);
    const task = taskPayload.task || {};
    const index = comfyui.tasks.findIndex((row) => row.id === task.id);
    if (index >= 0) comfyui.tasks[index] = task;
    else comfyui.tasks.unshift(task);
    renderTasks();
    await renderLatestOutputs();
    if (!isTaskActive(task)) {
      stopPolling();
      comfyui.activeTaskId = "";
      void refreshHealth();
    }
  } catch (_) { /* 下个周期重试 */ }
}

// 卡片指纹：状态/进度/错误/时间任一变化才算「变了」，指纹相同就保留现有 DOM，
// 避免每次轮询整块重建造成闪屏。
function taskFingerprint(row) {
  const progress = row.progress || {};
  return JSON.stringify([
    row.workflow_name || "", row.status || "", row.detail || "", row.error || "",
    Number(progress.done || 0), Number(progress.total || 0), Number(progress.failed || 0),
    (row.result?.outputs || []).length,
    Number(row.created_at || 0), Number(row.finished_at || 0),
    Boolean(row.can_retry), Boolean(row.can_cancel),
    comfyui.deleting.has(String(row.id || "")) ? "deleting" : "",
  ]);
}

function renderTasks() {
  const list = $("#comfyuiTaskList");
  if (!list) return;
  const rows = comfyui.tasks.slice(0, 12);
  if (!rows.length) {
    if (!list.dataset.empty) {
      list.textContent = "";
      list.dataset.empty = "1";
      const empty = document.createElement("p");
      empty.className = "comfyui-empty";
      empty.textContent = "还没有任务。选好工作流和参数后点「开始生成」。";
      list.append(empty);
    }
    return;
  }
  delete list.dataset.empty;
  const existing = new Map();
  for (const node of Array.from(list.children)) {
    if (node.dataset?.taskId) existing.set(node.dataset.taskId, node);
    else node.remove(); // 空态占位
  }
  const wanted = [];
  for (const row of rows) {
    const id = String(row.id || "");
    const fingerprint = taskFingerprint(row);
    let card = existing.get(id);
    if (!card || card.dataset.fingerprint !== fingerprint) {
      // 先摘掉旧卡片再放新的：map 引用被替换后旧节点不再被任何清理步骤覆盖。
      card?.remove();
      card = renderTaskCard(row);
      card.dataset.taskId = id;
      card.dataset.fingerprint = fingerprint;
      existing.set(id, card);
    }
    wanted.push(id);
  }
  const wantedSet = new Set(wanted);
  for (const [id, node] of existing) {
    if (!wantedSet.has(id)) node.remove();
  }
  // 只在顺序真的变化时才重排：无变化轮询不触碰 DOM。
  const currentOrder = Array.from(list.children).map((node) => node.dataset.taskId || "");
  if (currentOrder.join("|") !== wanted.join("|")) {
    for (const id of wanted) list.append(existing.get(id));
  }
}

function renderTaskCard(row) {
  const card = document.createElement("div");
  card.className = `comfyui-task-card status-${row.status}`;
  card.dataset.taskId = row.id;
  const head = document.createElement("div");
  head.className = "comfyui-task-head";
  const name = document.createElement("strong");
  name.textContent = row.workflow_name || "任务";
  const badge = document.createElement("span");
  badge.className = "comfyui-task-status";
  badge.textContent = statusText(row);
  head.append(name, badge);
  const meta = document.createElement("div");
  meta.className = "comfyui-task-meta";
  meta.textContent = taskMetaText(row);
  card.append(head, meta);
  if (row.error) {
    const errorLine = document.createElement("p");
    errorLine.className = "comfyui-task-error";
    errorLine.textContent = row.error;
    card.append(errorLine);
  }
  if (row.detail && !row.error) {
    const detail = document.createElement("p");
    detail.className = "comfyui-task-detail";
    detail.textContent = row.detail;
    card.append(detail);
  }
  const actions = document.createElement("div");
  actions.className = "comfyui-task-actions";
  if (row.status === "queued" || row.status === "running") {
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "control-button tiny";
    cancel.dataset.comfyuiCancel = row.id;
    cancel.textContent = "取消";
    actions.append(cancel);
  } else if (row.status === "failed" || row.status === "cancelled" || row.status === "interrupted") {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "control-button tiny";
    retry.dataset.comfyuiRetry = row.id;
    retry.textContent = "重试续跑";
    actions.append(retry);
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "control-button tiny";
  remove.dataset.comfyuiDeleteTask = row.id;
  if (comfyui.deleting.has(String(row.id || ""))) {
    remove.disabled = true;
    remove.textContent = "正在删除…";
  } else {
    remove.textContent = "删除";
  }
  actions.append(remove);
  card.append(actions);
  return card;
}

function statusText(row) {
  const map = {
    queued: "排队中",
    running: "生成中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
  };
  return map[row.status] || row.status || "";
}

// 时间显示口径：当天只报时分，跨天补上日期；时间戳缺失不显示。
function formatTime(ms) {
  const stamp = Number(ms || 0);
  if (!stamp) return "";
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value) => String(value).padStart(2, "0");
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (date.toDateString() === new Date().toDateString()) return clock;
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock}`;
}

function taskMetaText(row) {
  const progress = row.progress || {};
  const done = Number(progress.done || 0);
  const total = Number(progress.total || row.shots || 0);
  const failed = Number(progress.failed || 0);
  const pieces = [];
  const created = formatTime(row.created_at);
  if (created) pieces.push(`创建 ${created}`);
  pieces.push(`${done}/${total} 段`);
  if (failed) pieces.push(`失败 ${failed} 段`);
  const outputs = (row.result?.outputs || []).length;
  if (outputs) pieces.push(`${outputs} 个产物`);
  // 完成/失败/取消/中断都算结束：结束时刻标出这一轮什么时候停的。
  const finished = formatTime(row.finished_at);
  if (finished && row.status !== "queued" && row.status !== "running") {
    pieces.push(`结束 ${finished}`);
  }
  return pieces.join(" · ");
}

async function onTaskListClick(event) {
  const cancel = event.target.closest("[data-comfyui-cancel]");
  if (cancel) {
    try {
      await api(`/api/comfyui/tasks/${encodeURIComponent(cancel.dataset.comfyuiCancel)}/cancel`,
        { method: "POST", body: {} });
      await refreshTasks();
    } catch (error) {
      toast(`取消失败：${error.message}`);
    }
    return;
  }
  const retry = event.target.closest("[data-comfyui-retry]");
  if (retry) {
    try {
      const payload = await api(
        `/api/comfyui/tasks/${encodeURIComponent(retry.dataset.comfyuiRetry)}/retry`,
        { method: "POST", body: {} });
      comfyui.activeTaskId = String(payload.task?.id || "");
      comfyui.eventSeq = 0;
      await refreshTasks();
      startPolling();
    } catch (error) {
      toast(`重试失败：${error.message}`);
    }
    return;
  }
  const remove = event.target.closest("[data-comfyui-delete-task]");
  if (remove) {
    await deleteTaskFlow(remove.dataset.comfyuiDeleteTask, remove);
  }
}

async function deleteTaskFlow(taskId, button) {
  const id = String(taskId || "");
  if (!id || comfyui.deleting.has(id)) return;
  const row = comfyui.tasks.find((item) => String(item.id) === id);
  const active = row ? isTaskActive(row) : false;
  const confirmText = active
    ? "这条任务还在排队或生成中。会先取消它，等它停下来再删除任务和已保存的产物文件。继续吗？"
    : "删除这条任务和它已保存的产物文件？";
  if (!window.confirm(confirmText)) return;
  comfyui.deleting.add(id);
  if (button) {
    button.disabled = true;
    button.textContent = "正在删除…";
  }
  try {
    const payload = await api("/api/comfyui/tasks", {
      method: "DELETE",
      body: { task_ids: [id], cancel_active: true },
    });
    comfyui.deleting.delete(id);
    const failed = Array.isArray(payload.failed) ? payload.failed : [];
    if (failed.length) {
      toast(`没有删成：${failed[0]?.reason || "任务没能停下来"}`);
    } else {
      toast(active ? "已取消并删除这条任务" : "已删除这条任务");
    }
    if (comfyui.activeTaskId === id) {
      comfyui.activeTaskId = "";
      stopPolling();
    }
    await refreshTasks();
  } catch (error) {
    comfyui.deleting.delete(id);
    toast(`删除失败：${error.message}`);
    await refreshTasks();
  }
}

// 请求 ComfyUI 卸载已加载模型，释放显存/内存。ComfyUI 不打断执行中的任务：
// 还有任务在跑时它会在队列空下来才卸载，文案要把「稍后生效」讲清楚。
async function freeModels() {
  const button = $("#comfyuiFreeModels");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/comfyui/free", { method: "POST", body: {} });
    toast(payload?.pending
      ? "已请求释放模型：还有任务在执行，ComfyUI 会在任务结束后卸载"
      : "已请求 ComfyUI 释放模型，显存和内存会随之释放");
  } catch (error) {
    toast(`释放失败：${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

// 在运行 Cat Chat 的电脑上打开产物根目录（手机端会被服务端拒绝）。
async function openOutputsFolder() {
  const button = $("#comfyuiOpenOutputs");
  if (button) button.disabled = true;
  try {
    const payload = await api("/api/comfyui/outputs/open", { method: "POST", body: {} });
    toast(payload?.path ? `已打开产物文件夹：${payload.path}` : "已打开产物文件夹");
  } catch (error) {
    toast(`无法打开产物文件夹：${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

async function clearFinishedTasks() {
  if (!window.confirm("删除全部已结束的任务和它们的产物文件？")) return;
  try {
    const payload = await api("/api/comfyui/tasks", { method: "DELETE", body: {} });
    const parts = [`已删除 ${payload.deleted || 0} 条任务`];
    if (payload.skipped?.length) parts.push(`${payload.skipped.length} 条正在执行，已跳过`);
    const failed = Array.isArray(payload.failed) ? payload.failed : [];
    if (failed.length) parts.push(`${failed.length} 条删除失败：${failed[0]?.reason || ""}`);
    toast(parts.join("；"));
    await refreshTasks();
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

// ---- 产物 ----

async function renderLatestOutputs() {
  const grid = $("#comfyuiOutputGrid");
  if (!grid) return;
  // 每个产物各自记着它的任务 id：不同任务的产物用各自的文件地址，
  // 同一任务的多张图逐项全展示（上限沿用 24 张）。
  const items = [];
  for (const row of comfyui.tasks) {
    // output_files 带本地绝对路径；老任务没有这个字段时退回任务 id + 文件名的直链。
    const outputs = row.result?.outputs || [];
    const files = Array.isArray(row.output_files) && row.output_files.length
      ? row.output_files
      : outputs.map((name) => ({ name, path: "" }));
    for (const file of files) {
      if (items.length >= 24) break;
      const name = String(file?.name || "");
      if (!row.id || !name) continue;
      items.push({ taskId: String(row.id), name, path: String(file?.path || "") });
    }
  }
  const wanted = [];
  const seen = new Set();
  for (const item of items) {
    const key = `${item.taskId}\u0000${item.name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    wanted.push({ key, item });
  }
  if (!wanted.length) {
    if (!grid.dataset.empty) {
      grid.textContent = "";
      grid.dataset.empty = "1";
      const empty = document.createElement("p");
      empty.className = "comfyui-empty";
      empty.textContent = "还没有生成结果。提交任务后，出好的图会出现在这里。";
      grid.append(empty);
    }
    return;
  }
  delete grid.dataset.empty;
  const existing = new Map();
  for (const node of Array.from(grid.children)) {
    if (node.dataset?.outputKey) existing.set(node.dataset.outputKey, node);
    else node.remove(); // 空态占位
  }
  for (const { key, item } of wanted) {
    if (!existing.has(key)) {
      const card = renderOutputCard(item.taskId, item.name, item.path);
      card.dataset.outputKey = key;
      existing.set(key, card);
    }
  }
  for (const [key, node] of existing) {
    if (!seen.has(key)) node.remove();
  }
  // 只在产物集合或顺序变化时才重排：无变化轮询不触碰媒体元素（图片不重载）。
  const wantedOrder = wanted.map((entry) => entry.key);
  const currentOrder = Array.from(grid.children).map((node) => node.dataset.outputKey || "");
  if (currentOrder.join("|") !== wantedOrder.join("|")) {
    for (const key of wantedOrder) grid.append(existing.get(key));
  }
}

// paint 由 CSS 的 .comfyui-output-save svg 单点声明（同 .file-action 口径）。
const OUTPUT_SAVE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10"></path><path d="M8 11l4 4 4-4"></path><path d="M5 19h14"></path></svg>';

// 产物地址与聊天媒体卡片同一口径：走 /api/file（URL 自带口令 + 真实路径）。
// 只拼 /api/comfyui/tasks/<id>/files/<name> 的话，手机/局域网访问一律 401 成破图，
// 桌面的原生「另存为」也拿不到 path 参数（见 03-media 的 saveLocalFile）。
function outputFileUrl(taskId, name, path) {
  const local = String(path || "");
  if (local) return localFileUrl(local);
  const direct = `/api/comfyui/tasks/${encodeURIComponent(taskId)}/files/${encodeURIComponent(name)}`;
  return state.token ? `${direct}?token=${encodeURIComponent(state.token)}` : direct;
}

// 产物卡与聊天媒体卡同一套类型判定与展示语义（计划 §6）：图片缩略+灯箱、
// 视频/音频就地播放、其他类型文件 chip。mediaKind 走 /api/bootstrap.media_exts
// 的公共名单，不再各写一份扩展名正则（.bmp/.svg 曾因名单漂移彻底不可见）。
function renderOutputCard(taskId, name, path) {
  const url = outputFileUrl(taskId, name, path);
  const card = document.createElement("figure");
  card.className = "comfyui-output";
  const kind = mediaKind(String(path || url), name);
  if (kind === "image") {
    const img = document.createElement("img");
    // 造梦机产物没有侧车缩略图（attachmentThumbUrl 会推导 <stem>_thumb.webp 而
    // 404 破图），直接用原图地址，靠 loading=lazy 控制加载压力。
    img.src = url;
    img.alt = name;
    img.loading = "lazy";
    img.dataset.largeUrl = url;
    img.dataset.comfyuiOutputName = name;
    card.append(img);
  } else if (kind === "video") {
    const video = document.createElement("video");
    video.src = url;
    video.controls = true;
    video.playsInline = true;
    video.preload = "metadata";
    card.append(video);
  } else if (kind === "audio") {
    // 音频产物此前退化为下载链接；现在与聊天媒体卡一致，就地播放。
    const audio = document.createElement("audio");
    audio.src = url;
    audio.controls = true;
    audio.preload = "metadata";
    card.append(audio);
  } else {
    const link = document.createElement("a");
    link.href = url;
    link.textContent = name;
    link.download = name;
    card.append(link);
  }
  const caption = document.createElement("figcaption");
  const label = document.createElement("span");
  label.className = "comfyui-output-name";
  label.textContent = name;
  label.title = name;
  // 手机上点图只是开大图，保存要另找入口；这里给每张产物一个直接的下载按钮。
  const save = document.createElement("button");
  save.type = "button";
  save.className = "comfyui-output-save";
  save.dataset.comfyuiOutputSave = url;
  save.dataset.comfyuiOutputName = name;
  save.title = `下载 ${name}`;
  save.setAttribute("aria-label", `下载 ${name}`);
  save.innerHTML = OUTPUT_SAVE_ICON;
  caption.append(label, save);
  card.append(caption);
  return card;
}

function onOutputGridClick(event) {
  const save = event.target.closest("button[data-comfyui-output-save]");
  if (save) {
    if (!saveLocalFile(save.dataset.comfyuiOutputSave || "", save.dataset.comfyuiOutputName || "")) {
      toast("这个产物没有可下载的地址");
    }
    return;
  }
  // 图片放大走 03-media 的 document 级 data-large-url 委托（与聊天媒体卡同一入口），
  // 这里不再自己开一次灯箱，避免双触发。
  const link = event.target.closest("a[href]");
  if (link && isPywebview()) {
    void saveLocalFile(link.href, link.textContent || "output.bin");
  }
}

// ============================================================
// 分镜队列（创作场「分镜生图」跨页交接）：逐条填进正向提示词，不做剪贴板中转。
// 队列只存内存：刷新页面即清空，产物不在这里长期保存。
// ============================================================

function onStoryboardHandoff(event) {
  const raw = Array.isArray(event.detail?.items) ? event.detail.items : [];
  const items = raw
    .map((item, index) => ({
      no: Number(item?.no) || index + 1,
      head: String(item?.head || ""),
      prompt: String(item?.prompt || "").trim(),
    }))
    .filter((item) => item.prompt);
  if (!items.length) return;
  comfyui.storyboardQueue = items;
  renderStoryboardQueue();
  if (currentStudioPage() !== "comfyui") setStudioPage("comfyui");
  toast(`已带入 ${items.length} 条分镜提示词；选好工作流后逐条「填入」出图`);
}

function renderStoryboardQueue() {
  const box = $("#comfyuiStoryboard");
  const list = $("#comfyuiStoryboardList");
  if (!box || !list) return;
  const items = comfyui.storyboardQueue;
  box.hidden = !items.length;
  list.replaceChildren(...items.map((item, index) => {
    const row = document.createElement("div");
    row.className = "comfyui-storyboard-item";
    row.classList.toggle("filled", Boolean(item.filled));
    const fill = document.createElement("button");
    fill.type = "button";
    fill.className = "control-button tiny";
    fill.dataset.comfyuiStoryboardFill = String(index);
    fill.textContent = item.filled ? "再填入" : "填入";
    fill.title = "把这条提示词写进当前工作流的正向提示词框";
    const copy = document.createElement("span");
    copy.className = "comfyui-storyboard-copy";
    const head = document.createElement("strong");
    head.textContent = `第 ${item.no} 场${item.head ? ` · ${item.head}` : ""}`;
    const small = document.createElement("small");
    small.textContent = item.prompt;
    copy.append(head, small);
    row.append(fill, copy);
    return row;
  }));
}

function fillStoryboardPrompt(index) {
  const item = comfyui.storyboardQueue[index];
  if (!item) return;
  const slot = (comfyui.detail?.params || []).find((row) => row?.role === "positive_prompt");
  if (!comfyui.detail || !slot) {
    toast("先在左侧选择一个工作流，再填入分镜提示词");
    return;
  }
  const key = `${slot.node_id}|${slot.field_name}`;
  const area = document.querySelector(`[data-comfyui-slot="${CSS.escape(key)}"]`);
  if (!area) {
    toast("这个工作流的主表单里没有正向提示词框；用「编辑参数」把它加进来再填");
    return;
  }
  area.value = item.prompt;
  area.dispatchEvent(new Event("input", { bubbles: true }));
  item.filled = true;
  renderStoryboardQueue();
  toast(`已填入第 ${item.no} 场的提示词，确认参数后点「开始生成」`);
}
