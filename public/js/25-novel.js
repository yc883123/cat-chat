// ============================================================
// 25-novel.js —— 工作台「创作场」功能页（小说改编，阶段一）
// 导入 TXT → 自动拆章（零 AI）→ 勾选章节 → 保存为作品 → 单章查看/编辑 → 导出。
//
// 边界（《小说改编工作台总计划》§5.1、§5.6）：
// - 两段式：列表与详情只拿索引，正文只有打开某一章时才请求单章。
// - 阶段一是文件处理，不创建 Studio 任务；要素/大纲/剧本三档留到阶段二、三。
// - 与聊天域零耦合：只调 /api/novel/*。
// - 只从 01-core / 22-studio 取公共能力，22 不反向 import 本文件，无循环依赖。
// ============================================================

import { $, api, state, toast } from "./01-core.js";
import { onStudioPageChange } from "./22-studio.js";

// 与服务端一致的上限（前端只做提前提示，服务端仍逐项校验）。
const MAX_FILE_BYTES = 60 * 1024 * 1024;
const MAX_CHAPTERS = 9999;

const LEVEL_LABELS = {
  heading: "按章节标题识别",
  sep: "按符号分节（没有识别到章节标题）",
  single: "只识别到 1 个标题，仍按它切分",
  len: "没有识别到任何标题，按字数等分",
};

const novel = {
  initialized: false,
  // 作品态：bookId 为空表示还没打开作品。
  bookId: "",
  book: null,
  chapters: [],
  broken: [],
  // 当前打开的章节（正文只在打开时取）。
  chapterNo: 0,
  chapter: null,
  dirty: false,
  // 导入态：暂存里等勾选的章节。
  staged: null,
  picked: new Set(),
  filter: "",
  pane: "editor",
  busy: false,
  // 阶段（拆章 / 要素 / 大纲）：阶段二起要素与大纲共用同一套生成面板。
  stage: "split",
  // 生成态：任务只取创作场分区（module=novel），与模块库互不串台。
  tasks: [],
  activeTaskId: "",
  taskCursor: 0,
  taskPollTimer: null,
  taskPollBusy: false,
  streamText: "",
  stageWarnings: [],
  stageBusy: false,
  // 模型选择（与提词库同口径）：先选供应商，再按供应商拉完整模型目录。
  stageProviderKey: "",
  stageModelName: "",
  stageModelsCache: new Map(),
  stageModelInflight: new Map(),
  // 剧本阶段：当前正在编辑的场次号（0 = 未在编辑）。
  sceneEditing: 0,
  sceneBusy: false,
  // 迟到响应保护：每次切换作品/章节都换令牌，旧响应回来直接丢掉。
  bookToken: 0,
  chapterToken: 0,
  booksToken: 0,
  taskToken: 0,
};

// 阶段二起：一次生成最多带几章（与服务端 prompts.MAX_CHAPTERS_PER_CALL 一致）。
const MAX_STAGE_CHAPTERS = 6;
const STAGE_TASKS_URL = "/api/studio/tasks?module=novel";
const TASK_STATUS_LABELS = {
  queued: "排队中",
  running: "生成中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const TERMINAL_TASK_STATUSES = new Set(["done", "failed", "cancelled", "interrupted"]);
const STAGE_COPY = {
  elements: {
    title: "要素",
    hint: "从勾选章节整理人物、场景和道具",
    savedLabel: "作品里已保存的要素",
    empty: "还没有生成要素。勾选章节后点「生成」。",
  },
  outline: {
    title: "改编大纲",
    hint: "把勾选章节整理成三幕结构",
    savedLabel: "作品里已保存的大纲",
    empty: "还没有生成大纲。勾选章节后点「生成」。",
  },
  script: {
    title: "剧本",
    hint: "把勾选章节改编成可编辑的剧本场次",
    savedLabel: "已保存的场次",
    empty: "还没有生成剧本。勾选章节后点「生成」。",
  },
};

// ============================================================
// 小工具
// ============================================================

function fmtChars(count) {
  const value = Number(count) || 0;
  if (value >= 10000) return `${(value / 10000).toFixed(1)} 万`;
  return String(value);
}

function setSaveState(text, note = "") {
  const stateEl = $("#novelSaveState");
  if (stateEl) stateEl.textContent = text;
  const noteEl = $("#novelSaveNote");
  if (noteEl && note) noteEl.textContent = note;
}

function setBusy(busy) {
  novel.busy = busy;
  const btn = $("#novelSaveBook");
  if (btn) btn.disabled = busy || !novel.staged;
}

/** 统一错误提示：错误来自服务端的用户可读中文，直接展示，不吞不猜。 */
function fail(prefix, error) {
  const message = String(error?.message || error || "未知错误");
  toast(`${prefix}：${message}`);
  return message;
}

// ============================================================
// 渲染：章节列表
// ============================================================

function visibleChapters() {
  const keyword = novel.filter.trim().toLowerCase();
  if (!keyword) return novel.chapters;
  return novel.chapters.filter((row) => String(row.title || "").toLowerCase().includes(keyword));
}

function renderChapters() {
  const list = $("#novelChapterList");
  if (!list) return;
  const empty = $("#novelChapterEmpty");
  // 导入暂存态：列表显示待勾选的章节（复选框可点）；作品态：显示已保存章节。
  if (novel.staged) {
    const rows = novel.staged.chapters || [];
    const keyword = novel.filter.trim().toLowerCase();
    const shown = keyword
      ? rows.filter((row) => String(row.title || "").toLowerCase().includes(keyword))
      : rows;
    list.replaceChildren(...shown.map(stagedRow));
    if (empty) {
      empty.hidden = shown.length > 0;
      empty.textContent = rows.length
        ? "没有匹配的章节。换个关键词试试。"
        : "这本书里没有拆出章节。";
    }
    updatePickedCount();
    return;
  }
  const rows = visibleChapters();
  list.replaceChildren(...rows.map(chapterRow));
  if (empty) {
    empty.hidden = rows.length > 0;
    empty.textContent = novel.chapters.length
      ? "没有匹配的章节。换个关键词试试。"
      : "还没有章节。点上面的「导入 TXT」选一本小说。";
  }
  updatePickedCount();
}

function stagedRow(row) {
  const label = document.createElement("label");
  label.className = "novel-chapter-row";
  const checked = novel.picked.has(row.i);
  label.classList.toggle("picked", checked);
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = checked;
  box.dataset.novelPick = String(row.i);
  box.setAttribute("aria-label", `选择 ${row.title}`);
  const copy = document.createElement("span");
  copy.className = "novel-chapter-copy";
  const title = document.createElement("strong");
  title.textContent = row.title;
  const meta = document.createElement("small");
  meta.textContent = `${fmtChars(row.len)} 字`;
  copy.append(title, meta);
  const no = document.createElement("span");
  no.className = "novel-chapter-no";
  no.textContent = String(row.i).padStart(2, "0");
  label.append(box, copy, no);
  return label;
}

function chapterRow(row) {
  // 拆章阶段：点一行 = 打开这一章编辑。
  // 要素/大纲阶段：同一行要能勾选当素材，所以换成带复选框的 label（点行=勾选）。
  if (novel.stage !== "split") {
    const label = stagedRow(row);
    label.dataset.novelChapter = String(row.i);
    label.classList.toggle("active", row.i === novel.chapterNo);
    return label;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "novel-chapter-row";
  button.dataset.novelChapter = String(row.i);
  button.classList.toggle("active", row.i === novel.chapterNo);
  const copy = document.createElement("span");
  copy.className = "novel-chapter-copy";
  const title = document.createElement("strong");
  title.textContent = row.title;
  const meta = document.createElement("small");
  meta.textContent = `${fmtChars(row.len)} 字`;
  copy.append(title, meta);
  const no = document.createElement("span");
  no.className = "novel-chapter-no";
  no.textContent = String(row.i).padStart(2, "0");
  button.append(copy, no);
  return button;
}

function updatePickedCount() {
  const el = $("#novelSelectedCount");
  if (!el) return;
  el.textContent = novel.staged
    ? `已选 ${novel.picked.size} / ${novel.staged.chapters.length} 章`
    : `共 ${novel.chapters.length} 章`;
}

function renderBookSummary() {
  const book = novel.book;
  const staged = novel.staged;
  const titleEl = $("#novelBookTitle");
  const metaEl = $("#novelBookMeta");
  const cover = $("#novelBookCover");
  if (staged) {
    // 书名去掉扩展名：文件叫「长夜灯.txt」，作品名应该是「长夜灯」。
    const bookName = String(staged.name || "").replace(/\.[^.]+$/, "") || staged.name;
    if (titleEl) titleEl.textContent = `《${bookName}》`;
    if (metaEl) {
      metaEl.textContent =
        `${staged.total} 章 · 编码 ${staged.encoding} · ${LEVEL_LABELS[staged.split_level] || staged.split_level}`;
    }
    if (cover) cover.textContent = "待";
    const stats = [$("#novelStatChapters"), $("#novelStatChars"), $("#novelStatState")];
    if (stats[0]) stats[0].textContent = String(staged.total);
    if (stats[1]) stats[1].textContent = fmtChars(staged.total_len);
    if (stats[2]) stats[2].textContent = "待保存";
    return;
  }
  if (book) {
    if (titleEl) titleEl.textContent = `《${book.title}》`;
    if (metaEl) {
      const source = book.source || {};
      const parts = [source.encoding ? `编码 ${source.encoding}` : ""];
      if (source.split_level) parts.push(LEVEL_LABELS[source.split_level] || source.split_level);
      metaEl.textContent = parts.filter(Boolean).join(" · ") || "已保存作品";
    }
    if (cover) cover.textContent = "创";
    const stats = [$("#novelStatChapters"), $("#novelStatChars"), $("#novelStatState")];
    if (stats[0]) stats[0].textContent = String(book.chapter_count);
    if (stats[1]) stats[1].textContent = fmtChars(book.source?.total_len || 0);
    if (stats[2]) stats[2].textContent = novel.broken.length ? "数据异常" : "已保存";
    return;
  }
  if (titleEl) titleEl.textContent = "还没有打开作品";
  if (metaEl) metaEl.textContent = "导入一本 TXT 即可开始";
  if (cover) cover.textContent = "创";
  const stats = [$("#novelStatChapters"), $("#novelStatChars"), $("#novelStatState")];
  if (stats[0]) stats[0].textContent = "0";
  if (stats[1]) stats[1].textContent = "0";
  if (stats[2]) stats[2].textContent = "未导入";
}

function renderImportBlock() {
  const block = $("#novelImportBlock");
  if (!block) return;
  block.hidden = !novel.staged;
  if (!novel.staged) return;
  const staged = novel.staged;
  const stateEl = $("#novelImportState");
  if (stateEl) stateEl.textContent = "待保存";
  const note = $("#novelImportNote");
  if (note) {
    note.textContent =
      `共 ${staged.total} 章、约 ${fmtChars(staged.total_len)} 字。` +
      `拆分层级：${LEVEL_LABELS[staged.split_level] || staged.split_level}。` +
      `编码 ${staged.encoding} 是实际选中的解码方式，不代表源文件就是这个编码。` +
      "勾选要保留的章节，再点「保存为作品」。";
  }
}

function renderEditor() {
  const placeholder = $("#novelEditorPlaceholder");
  const fields = $("#novelEditorFields");
  const stagePanel = $("#novelStagePanel");
  const head = $("#novelEditorHead");
  const hint = $("#novelEditorHint");
  const chapter = novel.chapter;
  const stageMode = novel.stage !== "split";
  // 阶段二：编辑区换成生成面板（要素 / 大纲），章节编辑整体让位。
  if (stagePanel) stagePanel.hidden = !stageMode;
  if (fields) fields.hidden = stageMode || !chapter;
  if (placeholder) placeholder.hidden = stageMode || Boolean(chapter);
  if (stageMode) {
    const copy = STAGE_COPY[novel.stage];
    if (head) head.textContent = copy.title;
    if (hint) hint.textContent = copy.hint;
    renderStagePanel();
    return;
  }
  if (!chapter) {
    if (head) head.textContent = "章节编辑";
    if (hint) hint.textContent = novel.staged ? "勾选章节后保存为作品，再打开单章" : "选一章开始";
    return;
  }
  if (head) head.textContent = "章节编辑";
  if (hint) hint.textContent = `第 ${chapter.i} 章 / 共 ${novel.chapters.length} 章`;
  const titleInput = $("#novelChapterTitle");
  if (titleInput && document.activeElement !== titleInput) titleInput.value = chapter.title;
  const bodyInput = $("#novelChapterBody");
  if (bodyInput && document.activeElement !== bodyInput) bodyInput.value = chapter.content;
  const meta = $("#novelEditorMeta");
  if (meta) {
    meta.replaceChildren(
      metaItem("字数", String(chapter.len)),
      metaItem("版本", `第 ${chapter.revision} 版`),
      metaItem("状态", novel.dirty ? "有未保存的修改" : "已保存"),
    );
  }
  const save = $("#novelSaveChapter");
  if (save) save.disabled = !novel.dirty || novel.busy;
  const revert = $("#novelRevertChapter");
  if (revert) revert.disabled = !novel.dirty;
  const del = $("#novelDeleteChapter");
  if (del) del.disabled = novel.busy || novel.chapters.length <= 1;
}

function metaItem(label, value) {
  const wrap = document.createElement("div");
  wrap.className = "novel-meta-item";
  const span = document.createElement("span");
  span.textContent = label;
  const b = document.createElement("b");
  b.textContent = value;
  wrap.append(span, b);
  return wrap;
}

// ============================================================
// 阶段二：要素 / 大纲生成
// ============================================================

function stageSavedText(stage) {
  if (!novel.book) return "";
  if (stage === "elements") {
    const elements = novel.book.elements || {};
    if (elements.raw) return String(elements.raw);
    const lines = [];
    const push = (label, rows) => {
      if (!rows || !rows.length) return;
      lines.push(`## ${label}`);
      for (const row of rows) lines.push(`· ${row}`);
    };
    push("人物", elements.characters);
    push("场景", elements.scenes);
    push("道具", elements.props);
    return lines.join("\n");
  }
  return String(novel.book.outline || "");
}

function stageSavedAt(stage) {
  if (!novel.book) return "";
  const ms = stage === "elements"
    ? Number(novel.book.elements?.updated_at || 0)
    : Number(novel.book.outline_updated_at || 0);
  if (!ms) return "";
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderStagePanel() {
  const stage = novel.stage;
  // 阶段标题与提示由中栏面板头承担（renderEditor 里写），这里只管内容区。

  // 素材勾选状态：用左栏已勾的章节；导入暂存态不允许生成。
  const picked = [...novel.picked].sort((a, b) => a - b);
  const ready = Boolean(novel.bookId) && picked.length > 0 && !novel.staged;
  const meta = $("#novelStageMeta");
  if (meta) {
    meta.replaceChildren(
      metaItem("作品", novel.book ? `《${novel.book.title}》` : "未打开"),
      metaItem("素材章节", picked.length ? `${picked.length} 章` : "未选择"),
      metaItem("上限", `最多 ${MAX_STAGE_CHAPTERS} 章`),
    );
  }
  const generate = $("#novelStageGenerate");
  if (generate) {
    generate.disabled = !ready || novel.stageBusy || picked.length > MAX_STAGE_CHAPTERS;
  }
  const warning = $("#novelStageWarning");
  if (warning) {
    const lines = [];
    if (novel.staged) lines.push("先把导入的章节保存为作品，再生成。");
    else if (!novel.bookId) lines.push("先打开或保存一个作品。");
    else if (!picked.length) lines.push("在左侧勾选至少一章作为素材。");
    else if (!novel.stageModelName) lines.push("还没有可用模型，请到设置里检查模型。");
    else if (picked.length > MAX_STAGE_CHAPTERS) {
      lines.push(`一次最多带 ${MAX_STAGE_CHAPTERS} 章（已选 ${picked.length} 章，多了 ${picked.length - MAX_STAGE_CHAPTERS} 章），请分批生成。`);
    }
    for (const note of novel.stageWarnings) lines.push(note);
    warning.textContent = lines.join(" ");
  }

  // 任务区：进行中显示进度与取消；完成显示结果与写回；失败显示原因与重试。
  const taskBox = $("#novelStageTask");
  const savedBox = $("#novelStageSaved");
  const task = novel.tasks.find((row) => row.id === novel.activeTaskId) || null;
  if (taskBox) taskBox.hidden = !task;
  if (task) {
    const stateEl = $("#novelStageTaskState");
    const status = String(task.status || "");
    if (stateEl) {
      stateEl.textContent = TASK_STATUS_LABELS[status] || "状态未知";
    }
    const note = $("#novelStageTaskNote");
    if (note) {
      const bits = [];
      if (status === "queued") bits.push("生成任务在后台排队，作品页面不会被锁住。");
      if (status === "running") bits.push("正在生成，可以切到别的阶段继续做别的事。");
      if (status === "failed" && task.error) bits.push(String(task.error));
      if (status === "cancelled") bits.push("任务已取消。");
      if (status === "interrupted") bits.push("服务重启让任务中断了，可以重试。");
      note.textContent = bits.join(" ");
    }
    const body = String(novel.streamText || task.result || task.result_partial || "");
    const result = $("#novelStageResult");
    if (result) result.textContent = body;
    const cancel = $("#novelStageCancel");
    if (cancel) cancel.hidden = TERMINAL_TASK_STATUSES.has(status);
    const retry = $("#novelStageRetry");
    if (retry) retry.hidden = !["failed", "cancelled", "interrupted"].includes(status);
    const apply = $("#novelStageApply");
    // 只有「已完成且结果非空」才能写回：写回是覆盖动作，不能拿空结果覆盖已有成果。
    if (apply) apply.hidden = !(status === "done" && body.trim());
  }

  // 「作品里已保存的结果」只给要素/大纲用；剧本阶段由场次列表承担，
  // 否则同一块区域会先把大纲原文摆出来、把场次挤到下面去。
  const saved = stage === "script" ? "" : stageSavedText(stage);
  if (savedBox) savedBox.hidden = !saved;
  if (saved) {
    const savedBody = $("#novelStageSavedBody");
    if (savedBody) savedBody.textContent = saved;
    const savedAt = $("#novelStageSavedAt");
    if (savedAt) savedAt.textContent = stageSavedAt(stage) || "";
  }
  renderSceneBlock(stage === "script");
}

// ============================================================
// 剧本场次（阶段三）
// ============================================================

function renderSceneBlock(visible) {
  const block = $("#novelSceneBlock");
  if (!block) return;
  block.hidden = !visible;
  if (!visible) return;
  const rows = Array.isArray(novel.book?.script) ? novel.book.script : [];
  const list = $("#novelSceneList");
  const count = $("#novelSceneCount");
  if (count) count.textContent = rows.length ? `共 ${rows.length} 场` : "";
  const note = $("#novelSceneNote");
  if (note) {
    if (!rows.length) {
      note.textContent = STAGE_COPY.script.empty;
    } else if (novel.book?.script_parsed === false) {
      // 解析失败时如实说明：整段保存，没有臆造场次切分。
      note.textContent = "上次生成的剧本没能识别出场次结构，已按整段保存；可以手动编辑成 INT./EXT. 场景行。";
    } else {
      note.textContent = "点一场可以编辑；↑↓ 调整顺序，改动会立刻保存。";
    }
  }
  if (!list) return;
  list.replaceChildren(...rows.map(sceneRow));
  renderSceneEdit();
}

function sceneRow(row) {
  const no = Number(row.i) || 0;
  const item = document.createElement("div");
  item.className = "novel-scene-row";
  item.classList.toggle("active", no === novel.sceneEditing);
  item.dataset.novelScene = String(no);
  const head = document.createElement("button");
  head.type = "button";
  head.className = "novel-scene-open";
  head.dataset.novelSceneOpen = String(no);
  const strong = document.createElement("strong");
  strong.textContent = String(row.scene || "（没有场景行）");
  const small = document.createElement("small");
  const chapter = Number(row.from_chapter) || 0;
  const bodyLen = String(row.content || "").length;
  small.textContent = `${chapter ? `第 ${chapter} 章` : "来源未记录"} · ${bodyLen} 字`;
  head.append(strong, small);
  const actions = document.createElement("span");
  actions.className = "novel-scene-actions";
  const mk = (label, action, title) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "control-button tiny";
    btn.dataset.novelSceneAction = action;
    btn.dataset.novelSceneNo = String(no);
    btn.title = title;
    btn.textContent = label;
    return btn;
  };
  actions.append(
    mk("↑", "up", "与上一场交换位置"),
    mk("↓", "down", "与下一场交换位置"),
    mk("删除", "delete", "删除这一场"),
  );
  item.append(head, actions);
  return item;
}

function renderSceneEdit() {
  const box = $("#novelSceneEdit");
  if (!box) return;
  const rows = Array.isArray(novel.book?.script) ? novel.book.script : [];
  const row = rows.find((item) => Number(item.i) === novel.sceneEditing);
  box.hidden = !row;
  if (!row) return;
  const headInput = $("#novelSceneHead");
  if (headInput && document.activeElement !== headInput) headInput.value = String(row.scene || "");
  const bodyInput = $("#novelSceneBody");
  if (bodyInput && document.activeElement !== bodyInput) bodyInput.value = String(row.content || "");
}

async function saveScene() {
  if (!novel.bookId || !novel.sceneEditing) return;
  const scene = String($("#novelSceneHead")?.value || "").trim();
  const content = String($("#novelSceneBody")?.value ?? "");
  novel.sceneBusy = true;
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/script/${novel.sceneEditing}`,
      { method: "PATCH", body: { scene, content, revision: novel.book?.revision } }
    );
    const rows = Array.isArray(novel.book?.script) ? [...novel.book.script] : [];
    const index = rows.findIndex((item) => Number(item.i) === novel.sceneEditing);
    if (index >= 0) rows[index] = { ...rows[index], ...payload.scene };
    novel.book = { ...novel.book, script: rows, revision: payload.revision };
    setSaveState("已保存", `第 ${novel.sceneEditing} 场已保存。`);
    renderAll();
    toast("场次已保存");
  } catch (error) {
    fail("保存场次失败", error);
  } finally {
    novel.sceneBusy = false;
  }
}

async function deleteScene(sceneNo) {
  if (!novel.bookId) return;
  if (!window.confirm(`要删除第 ${sceneNo} 场吗？删除后不能撤销。`)) return;
  try {
    await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/script/${sceneNo}`,
      { method: "DELETE" }
    );
    if (novel.sceneEditing === sceneNo) novel.sceneEditing = 0;
    // 编号会重排，必须从服务端重新读一次，不能只在前端删一条。
    const payload = await api(`/api/novel/books/${encodeURIComponent(novel.bookId)}/script`);
    novel.book = { ...novel.book, script: payload.script, revision: payload.revision };
    renderAll();
    toast(`已删除第 ${sceneNo} 场`);
  } catch (error) {
    fail("删除场次失败", error);
  }
}

async function moveScene(sceneNo, direction) {
  if (!novel.bookId) return;
  const rows = Array.isArray(novel.book?.script) ? novel.book.script : [];
  const index = rows.findIndex((item) => Number(item.i) === sceneNo);
  const target = direction === "up" ? index - 1 : index + 1;
  if (index < 0 || target < 0 || target >= rows.length) return;
  const order = rows.map((item) => Number(item.i));
  [order[index], order[target]] = [order[target], order[index]];
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/script/reorder`,
      { method: "POST", body: { order } }
    );
    novel.book = { ...novel.book, script: payload.script, revision: payload.revision };
    novel.sceneEditing = target + 1;
    renderAll();
  } catch (error) {
    fail("调整场次顺序失败", error);
  }
}

/**
 * 模型选择与提词库同口径：先选供应商，再选该供应商目录里的模型。
 *
 * 供应商列表来自 `bootstrap.model_profiles`（带 model_key，与提词库同一份）；
 * 模型列表对选中的供应商调 `POST /api/providers/models` 拉**完整目录**——
 * 只读卡片上那个 `model` 字段的话，一个供应商就只能选到一个模型。
 * 目录拉不到时（网络不通 / 供应商没有目录接口）回退到卡片上配置的模型，
 * 那是用户自己填的、能直接用的名字，比给一个空下拉框有用。
 */
function fillStageProviders() {
  const select = $("#novelStageProvider");
  if (!select) return;
  const profiles = Array.isArray(state.bootstrap?.model_profiles)
    ? state.bootstrap.model_profiles
    : (Array.isArray(state.bootstrap?.providers) ? state.bootstrap.providers : []);
  const rows = profiles
    .map((row) => ({
      key: String(row?.model_key || `online:${row?.id || ""}`),
      label: String(row?.name || row?.model_key || row?.id || ""),
    }))
    .filter((row) => row.key && row.label);
  const previous = select.value || novel.stageProviderKey;
  select.replaceChildren(...rows.map((row) => {
    const option = document.createElement("option");
    option.value = row.key;
    option.textContent = row.label;
    return option;
  }));
  if (!rows.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "请先在设置中添加模型";
    select.append(option);
    novel.stageProviderKey = "";
    return;
  }
  // 优先沿用上次选择，其次是会话当前模型，最后落到第一个。
  const preferred = [
    previous,
    $("#modelSelect")?.value || "",
    state.bootstrap?.default_model_key || "",
  ].find((key) => key && rows.some((row) => row.key === key));
  select.value = preferred || rows[0].key;
  novel.stageProviderKey = select.value;
}

/** 按当前供应商拉完整模型目录，填进模型下拉。 */
async function fillStageModelOptions() {
  const select = $("#novelStageModel");
  if (!select) return;
  const key = novel.stageProviderKey || $("#novelStageProvider")?.value || "";
  if (!key) {
    select.replaceChildren(new Option("请先在设置中添加模型", ""));
    novel.stageModelName = "";
    return;
  }
  let models = novel.stageModelsCache.get(key);
  if (!models) {
    // 同一供应商的并发请求只发一次（首屏与切页会连续触发两次），
    // 否则缓存还没写回就又发一遍，白拉一次目录。
    const inflight = novel.stageModelInflight.get(key);
    if (inflight) {
      models = await inflight;
    } else {
      const task = (async () => {
        try {
          const payload = await api("/api/providers/models", {
            method: "POST",
            body: { model_key: key },
          });
          return Array.isArray(payload?.models) ? payload.models : [];
        } catch (error) {
          return [];
        }
      })();
      novel.stageModelInflight.set(key, task);
      try {
        models = await task;
      } finally {
        novel.stageModelInflight.delete(key);
      }
      novel.stageModelsCache.set(key, models);
    }
  }
  const names = [];
  for (const entry of models) {
    const id = typeof entry === "object"
      ? String(entry?.id ?? entry?.model ?? entry?.name ?? "")
      : String(entry ?? "");
    const clean = id.trim();
    if (clean && !names.includes(clean)) names.push(clean);
  }
  if (!names.length) {
    // 目录读不到：回退到这张卡片上配置的模型名（用户自己填的，能直接用）。
    const profile = (state.bootstrap?.model_profiles || []).find((row) => row.model_key === key);
    const configured = String(profile?.model || "").trim();
    if (configured) names.push(configured);
  }
  const previous = novel.stageModelName || select.value;
  select.replaceChildren(...names.map((name) => new Option(name, name)));
  if (!names.length) {
    select.replaceChildren(new Option("模型目录读取失败，请到设置里检查", ""));
    novel.stageModelName = "";
    return;
  }
  select.value = names.includes(previous) ? previous : names[0];
  novel.stageModelName = select.value;
}

/**
 * 重画两级下拉（供应商同步 + 模型目录异步）。
 *
 * 串行化：首屏、进页、切阶段都会调它，并发进入时后一次等前一次跑完再走，
 * 这样目录只拉一次（缓存写回后再判断命中），不会因为「缓存还没写就重进」而重复请求。
 */
let stageModelsChain = Promise.resolve();
function fillStageModels() {
  stageModelsChain = stageModelsChain.then(async () => {
    fillStageProviders();
    await fillStageModelOptions();
  }).catch(() => {});
  return stageModelsChain;
}

function readStageModel() {
  return {
    model_key: novel.stageProviderKey || String($("#novelStageProvider")?.value || ""),
    model_name: novel.stageModelName || String($("#novelStageModel")?.value || ""),
  };
}

function stopTaskPolling() {
  if (novel.taskPollTimer) {
    clearTimeout(novel.taskPollTimer);
    novel.taskPollTimer = null;
  }
}

async function refreshStageTasks() {
  try {
    const { tasks } = await api(STAGE_TASKS_URL);
    novel.tasks = Array.isArray(tasks) ? tasks : [];
  } catch (error) {
    // 读不到任务列表不该挡住生成面板的其它操作，只如实提示。
    fail("读取创作场任务失败", error);
    return;
  }
  // 没有指定活动任务时，自动接上最近一条属于当前作品的同阶段任务。
  if (!novel.activeTaskId && novel.bookId) {
    const hit = novel.tasks.find((row) => {
      const info = row.novel || {};
      return info.book_id === novel.bookId && info.stage === novel.stage;
    });
    if (hit) {
      novel.activeTaskId = hit.id;
      novel.streamText = "";
      novel.taskCursor = 0;
    }
  }
  renderStagePanel();
  scheduleTaskPolling();
}

function scheduleTaskPolling() {
  stopTaskPolling();
  const task = novel.tasks.find((row) => row.id === novel.activeTaskId);
  if (!task || TERMINAL_TASK_STATUSES.has(String(task.status || ""))) return;
  novel.taskPollTimer = setTimeout(() => void pollActiveTask(), 1200);
}

async function pollActiveTask() {
  if (novel.taskPollBusy || !novel.activeTaskId) return;
  novel.taskPollBusy = true;
  const token = novel.taskToken;
  try {
    const { task } = await api(`/api/studio/tasks/${encodeURIComponent(novel.activeTaskId)}`);
    if (token !== novel.taskToken) return;
    const index = novel.tasks.findIndex((row) => row.id === task.id);
    if (index >= 0) novel.tasks[index] = { ...novel.tasks[index], ...task };
    else novel.tasks.unshift(task);
    // 增量事件只取正文片段，不重复拉全量结果。
    try {
      const events = await api(
        `/api/studio/tasks/${encodeURIComponent(novel.activeTaskId)}/events?after=${novel.taskCursor}`
      );
      for (const row of events.events || []) {
        if (row.type === "delta" && row.content) novel.streamText += String(row.content);
        if (typeof row.seq === "number") novel.taskCursor = Math.max(novel.taskCursor, row.seq);
      }
    } catch (_error) {
      // 事件流读不到时退回用任务记录里的 result_partial，不影响终态判断。
    }
    renderStagePanel();
  } catch (error) {
    if (token === novel.taskToken) fail("读取任务状态失败", error);
  } finally {
    novel.taskPollBusy = false;
    scheduleTaskPolling();
  }
}

async function generateStage() {
  if (!novel.bookId) return;
  const picked = [...novel.picked].sort((a, b) => a - b);
  if (!picked.length) {
    toast("先勾选至少一章作为素材");
    return;
  }
  if (picked.length > MAX_STAGE_CHAPTERS) {
    toast(`一次最多带 ${MAX_STAGE_CHAPTERS} 章，请分批生成`);
    return;
  }
  const model = readStageModel();
  if (!model.model_name) {
    toast("先在设置里检查模型，再回来选择");
    return;
  }
  novel.stageBusy = true;
  novel.stageWarnings = [];
  renderStagePanel();
  try {
    const payload = await api("/api/novel/stage", {
      method: "POST",
      body: {
        book_id: novel.bookId,
        stage: novel.stage,
        chapter_ids: picked,
        extra_note: String($("#novelStageNote")?.value || ""),
        model_key: model.model_key,
        model_name: model.model_name,
      },
    });
    novel.activeTaskId = payload.task?.id || "";
    novel.streamText = "";
    novel.taskCursor = 0;
    novel.stageWarnings = Array.isArray(payload.warnings) ? payload.warnings : [];
    if (payload.task) novel.tasks.unshift(payload.task);
    renderStagePanel();
    scheduleTaskPolling();
  } catch (error) {
    fail("提交生成失败", error);
  } finally {
    novel.stageBusy = false;
    renderStagePanel();
  }
}

async function cancelStageTask() {
  if (!novel.activeTaskId) return;
  try {
    await api(`/api/studio/tasks/${encodeURIComponent(novel.activeTaskId)}/cancel`, {
      method: "POST", body: {},
    });
    toast("已取消");
    await refreshStageTasks();
  } catch (error) {
    fail("取消失败", error);
  }
}

async function retryStageTask() {
  if (!novel.activeTaskId) return;
  try {
    // 重发的是当时素材快照：改了章节再重试不会带新素材（与工作台其它任务同口径）。
    const { task } = await api(`/api/studio/tasks/${encodeURIComponent(novel.activeTaskId)}/retry`, {
      method: "POST", body: {},
    });
    novel.activeTaskId = task?.id || "";
    novel.streamText = "";
    novel.taskCursor = 0;
    await refreshStageTasks();
    toast("已重新排队");
  } catch (error) {
    fail("重试失败", error);
  }
}

async function applyStageResult() {
  if (!novel.activeTaskId || !novel.bookId) return;
  const task = novel.tasks.find((row) => row.id === novel.activeTaskId);
  const text = String(novel.streamText || task?.result || task?.result_partial || "");
  if (!text.trim()) {
    toast("这个任务没有结果可写回");
    return;
  }
  const label = STAGE_COPY[novel.stage].title;
  if (!window.confirm(`把这次的${label}结果写回作品吗？作品里已有的${label}会被覆盖。`)) return;
  try {
    const payload = await api(`/api/novel/books/${encodeURIComponent(novel.bookId)}/apply`, {
      method: "POST",
      body: {
        stage: novel.stage,
        task_id: novel.activeTaskId,
        text,
        revision: novel.book?.revision,
      },
    });
    novel.book = payload.book;
    novel.activeTaskId = "";
    novel.streamText = "";
    novel.taskCursor = 0;
    novel.sceneEditing = 0;
    setSaveState("已保存", `${label}已写回作品。`);
    renderAll();
    toast(`${label}已写回作品`);
  } catch (error) {
    // 409 = 任务/阶段不符或作品已被改过；如实提示，不覆盖。
    fail(`写回${label}失败`, error);
  }
}

function setStage(stage) {
  const next = ["split", "elements", "outline", "script"].includes(stage) ? stage : "split";
  novel.stage = next;
  novel.stageWarnings = [];
  document.querySelectorAll("#novelStages [data-novel-stage]").forEach((btn) => {
    const active = btn.dataset.novelStage === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  if (next !== "split") {
    // 进阶段页：接上这个作品当前阶段的任务，避免「切回来什么也看不到」。
    void refreshStageTasks();
  } else {
    stopTaskPolling();
  }
  renderAll();
}

function renderAll() {
  renderBookSummary();
  renderChapters();
  renderImportBlock();
  renderEditor();
  const exportBtn = $("#novelExportBtn");
  if (exportBtn) exportBtn.disabled = !novel.bookId || novel.busy;
  const importBtn = $("#novelImportBtn");
  if (importBtn) importBtn.disabled = novel.busy;
}

// ============================================================
// 数据操作
// ============================================================

async function loadBooks() {
  try {
    const payload = await api("/api/novel/books");
    return payload?.books || [];
  } catch (error) {
    fail("读取作品列表失败", error);
    return [];
  }
}

async function openBook(bookId) {
  const token = ++novel.bookToken;
  novel.chapterToken += 1;
  novel.taskToken += 1;
  stopTaskPolling();
  try {
    const payload = await api(`/api/novel/books/${encodeURIComponent(bookId)}`);
    if (token !== novel.bookToken) return;
    novel.bookId = bookId;
    novel.book = payload.book;
    novel.chapters = payload.chapters || [];
    novel.broken = payload.broken || [];
    novel.staged = null;
    novel.picked.clear();
    novel.chapter = null;
    novel.chapterNo = 0;
    novel.dirty = false;
    novel.activeTaskId = "";
    novel.streamText = "";
    novel.taskCursor = 0;
    novel.stageWarnings = [];
    setSaveState("已读取", `作品《${novel.book.title}》共 ${novel.chapters.length} 章。` +
      (novel.broken.length ? `有 ${novel.broken.length} 章读取失败（数据异常），需要重新导入或删除。` : ""));
    renderAll();
    if (novel.chapters.length) void openChapter(novel.chapters[0].i);
    if (novel.stage !== "split") void refreshStageTasks();
  } catch (error) {
    if (token !== novel.bookToken) return;
    fail("打开作品失败", error);
    setSaveState("读取失败", String(error?.message || error));
  }
}

async function openChapter(chapterNo) {
  if (!novel.bookId) return;
  if (novel.dirty && !window.confirm("这一章有未保存的修改，切换后修改会丢失。要继续吗？")) return;
  const token = ++novel.chapterToken;
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/chapters/${chapterNo}`
    );
    if (token !== novel.chapterToken) return;
    novel.chapterNo = chapterNo;
    novel.chapter = payload;
    novel.dirty = false;
    setSaveState("已读取", `已打开第 ${chapterNo} 章（${payload.len} 字）。`);
    renderAll();
    if (novel.pane === "chapters") setPane("editor");
  } catch (error) {
    if (token !== novel.chapterToken) return;
    fail("读取章节失败", error);
  }
}

async function importFile(file) {
  if (!file) return;
  if (file.size > MAX_FILE_BYTES) {
    toast(`文件太大（${(file.size / 1024 / 1024).toFixed(1)} MB），上限 60 MB`);
    return;
  }
  setBusy(true);
  setSaveState("正在导入", `正在拆分《${file.name}》，请稍候。`);
  try {
    const form = new FormData();
    form.append("file", file);
    // 文件名单独给一份：multipart 头里的 filename 在服务端要按 latin-1 还原，
    // 这里显式传 UTF-8 字段更稳（中文书名不会变成问号）。
    form.append("filename", file.name);
    const payload = await api("/api/novel/import", { method: "POST", body: form });
    novel.staged = payload;
    novel.picked = new Set((payload.chapters || []).map((row) => row.i));
    novel.bookId = "";
    novel.book = null;
    novel.chapters = [];
    novel.broken = [];
    novel.chapter = null;
    novel.chapterNo = 0;
    novel.dirty = false;
    setSaveState("待保存", `拆出 ${payload.total} 章。勾选要保留的章节，再点「保存为作品」。`);
    renderAll();
  } catch (error) {
    fail("导入失败", error);
    setSaveState("导入失败", String(error?.message || error));
  } finally {
    setBusy(false);
    renderAll();
  }
}

async function saveBook() {
  if (!novel.staged) return;
  const picked = [...novel.picked].sort((a, b) => a - b);
  if (!picked.length) {
    toast("至少选择一章");
    return;
  }
  setBusy(true);
  setSaveState("正在保存", `正在保存 ${picked.length} 章…`);
  try {
    const payload = await api("/api/novel/books", {
      method: "POST",
      body: { import_id: novel.staged.import_id, picked, title: novel.staged.name.replace(/\.[^.]+$/, "") },
    });
    novel.staged = null;
    novel.picked.clear();
    await openBook(payload.book.id);
    toast(`已保存作品《${payload.book.title}》，共 ${payload.book.chapter_count} 章`);
  } catch (error) {
    fail("保存作品失败", error);
    setSaveState("保存失败", String(error?.message || error));
  } finally {
    setBusy(false);
    renderAll();
  }
}

async function saveChapter() {
  if (!novel.bookId || !novel.chapter) return;
  const titleInput = $("#novelChapterTitle");
  const bodyInput = $("#novelChapterBody");
  const title = String(titleInput?.value || "").trim();
  const content = String(bodyInput?.value ?? "");
  setBusy(true);
  setSaveState("正在保存", `正在保存第 ${novel.chapter.i} 章…`);
  try {
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/chapters/${novel.chapter.i}`,
      {
        method: "PATCH",
        body: { title, content, revision: novel.chapter.revision },
      }
    );
    novel.chapter = payload.chapter;
    novel.dirty = false;
    // 索引里的标题/字数要跟着更新，否则左侧列表还是旧值。
    const row = novel.chapters.find((item) => item.i === payload.chapter.i);
    if (row) {
      row.title = payload.chapter.title;
      row.len = payload.chapter.len;
    }
    if (novel.book) {
      novel.book.revision = (novel.book.revision || 0) + 1;
    }
    setSaveState("已保存", `第 ${payload.chapter.i} 章已保存（${payload.chapter.len} 字）。`);
    renderAll();
    toast("本章已保存");
  } catch (error) {
    // 409 = 别处已经改过（乐观并发），提示重新读取，不覆盖别人的写回。
    fail("保存章节失败", error);
    setSaveState("保存失败", String(error?.message || error));
  } finally {
    setBusy(false);
    renderAll();
  }
}

async function deleteChapter() {
  if (!novel.bookId || !novel.chapter) return;
  const no = novel.chapter.i;
  if (!window.confirm(`要从作品里删除第 ${no} 章吗？正文会一并删除，不能撤销。`)) return;
  setBusy(true);
  try {
    await api(`/api/novel/books/${encodeURIComponent(novel.bookId)}/chapters/${no}`, { method: "DELETE" });
    novel.chapter = null;
    novel.chapterNo = 0;
    novel.dirty = false;
    await openBook(novel.bookId);
    toast(`已删除第 ${no} 章`);
  } catch (error) {
    fail("删除章节失败", error);
  } finally {
    setBusy(false);
    renderAll();
  }
}

async function deleteBook(bookId, title) {
  if (!window.confirm(`要删除作品《${title}》吗？整本作品的章节都会删除，不能撤销。`)) return;
  try {
    await api(`/api/novel/books/${encodeURIComponent(bookId)}`, { method: "DELETE" });
    if (novel.bookId === bookId) {
      novel.bookId = "";
      novel.book = null;
      novel.chapters = [];
      novel.broken = [];
      novel.chapter = null;
      novel.chapterNo = 0;
      novel.dirty = false;
      setSaveState("—", "作品已删除。");
      renderAll();
    }
    toast("作品已删除");
    void renderBookDialog();
  } catch (error) {
    fail("删除作品失败", error);
  }
}

/**
 * 导出全书。
 * `fmt` = txt | md；`withExtras` 为真时附要素与大纲附录（阶段三）。
 * 整本拼装在服务端完成，前端只落 Blob——大书也不占浏览器内存做拼接。
 */
async function exportBook(fmt = "txt", withExtras = false) {
  if (!novel.bookId) return;
  try {
    const query = new URLSearchParams({ format: fmt });
    if (withExtras) query.set("with_extras", "1");
    const payload = await api(
      `/api/novel/books/${encodeURIComponent(novel.bookId)}/export?${query.toString()}`
    );
    const base = (novel.book?.title || "作品").replace(/[\\/:*?"<>|]/g, "_");
    const name = `${base}.${fmt}`;
    const mime = fmt === "md" ? "text/markdown;charset=utf-8" : "text/plain;charset=utf-8";
    // triggerDownload 只接受 URL；这里要落的是内存里的文本，直接造一个临时 blob 链接。
    const url = URL.createObjectURL(new Blob([payload.text || ""], { type: mime }));
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.rel = "noopener";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setSaveState("已导出", `已导出《${novel.book?.title || "作品"}》为 ${name}${withExtras ? "（含要素与大纲）" : ""}。`);
  } catch (error) {
    fail("导出失败", error);
  }
}

// ============================================================
// 作品列表弹窗
// ============================================================

async function renderBookDialog() {
  const list = $("#novelBookList");
  const errorEl = $("#novelBooksError");
  if (!list) return;
  if (errorEl) errorEl.textContent = "";
  list.replaceChildren(...[Object.assign(document.createElement("p"), {
    className: "novel-empty",
    textContent: "正在读取…",
  })]);
  const token = ++novel.booksToken;
  try {
    const books = await loadBooks();
    if (token !== novel.booksToken) return;
    if (!books.length) {
      list.replaceChildren(Object.assign(document.createElement("p"), {
        className: "novel-empty",
        textContent: "还没有保存过作品。先导入一本 TXT 并保存。",
      }));
      return;
    }
    list.replaceChildren(...books.map((book) => {
      const row = document.createElement("div");
      row.className = "novel-book-row";
      row.classList.toggle("active", book.id === novel.bookId);
      const open = document.createElement("button");
      open.type = "button";
      open.className = "novel-book-open";
      open.dataset.novelOpen = book.id;
      const strong = document.createElement("strong");
      strong.textContent = `《${book.title}》`;
      const small = document.createElement("small");
      small.textContent = `${book.chapter_count} 章 · ${fmtChars(book.source?.total_len || 0)} 字`;
      open.append(strong, small);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "control-button tiny danger";
      del.dataset.novelDeleteBook = book.id;
      del.dataset.novelDeleteTitle = book.title;
      del.textContent = "删除";
      row.append(open, del);
      return row;
    }));
  } catch (error) {
    if (token !== novel.booksToken) return;
    if (errorEl) errorEl.textContent = String(error?.message || error);
    list.replaceChildren();
  }
}

// 手机竖屏断点：与 styles.css 的 `@media (max-width: 760px)` 一致。
const PORTRAIT_QUERY = "(max-width: 760px)";

function isPaneLayout() {
  return typeof window.matchMedia === "function" && window.matchMedia(PORTRAIT_QUERY).matches;
}

function setPane(pane) {
  const next = ["chapters", "editor", "preview"].includes(pane) ? pane : "editor";
  novel.pane = next;
  const columns = $("#novelColumns");
  if (columns) columns.dataset.novelPane = next;
  document.querySelectorAll("#studioPageNovel [data-novel-pane]").forEach((btn) => {
    if (!btn.classList.contains("novel-pane-btn")) return;
    const active = btn.dataset.novelPane === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  applyPaneAccessibility();
}

/**
 * 隐藏的面板不可被键盘或辅助技术访问。
 * 只在竖屏分档布局下生效——桌面/横屏三栏全部可见，给其中一栏设 inert 会让它彻底点不动。
 */
function applyPaneAccessibility() {
  const paneLayout = isPaneLayout();
  const map = { chapters: ".novel-chapters", editor: ".novel-editor", preview: ".novel-preview" };
  for (const [key, selector] of Object.entries(map)) {
    const el = document.querySelector(`#studioPageNovel ${selector}`);
    if (!el) continue;
    const hidden = paneLayout && key !== novel.pane;
    el.setAttribute("aria-hidden", String(hidden));
    el.inert = hidden;
  }
}

function markDirty() {
  if (!novel.chapter || novel.dirty) return;
  novel.dirty = true;
  renderEditor();
}

// ============================================================
// 接线
// ============================================================

export function bindNovelEvents() {
  if (novel.initialized) return;
  novel.initialized = true;

  $("#novelImportBtn")?.addEventListener("click", () => $("#novelFileInput")?.click());
  $("#novelFileInput")?.addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void importFile(file);
  });
  $("#novelSaveBook")?.addEventListener("click", () => void saveBook());
  $("#novelCancelImport")?.addEventListener("click", () => {
    novel.staged = null;
    novel.picked.clear();
    setSaveState("—", "已放弃这次导入，没有保存任何内容。");
    renderAll();
  });
  $("#novelSaveChapter")?.addEventListener("click", () => void saveChapter());
  $("#novelRevertChapter")?.addEventListener("click", () => {
    if (!novel.chapter) return;
    const no = novel.chapter.i;
    novel.dirty = false;
    novel.chapter = null;
    void openChapter(no);
  });
  $("#novelDeleteChapter")?.addEventListener("click", () => void deleteChapter());
  // 导出：点按钮开菜单，菜单项决定格式与是否附要素/大纲。
  $("#novelExportBtn")?.addEventListener("click", () => {
    const menu = $("#novelExportMenu");
    if (!menu) return;
    const open = menu.hidden;
    menu.hidden = !open;
    $("#novelExportBtn")?.setAttribute("aria-expanded", String(open));
  });
  $("#novelExportMenu")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-novel-export]");
    if (!btn) return;
    const menu = $("#novelExportMenu");
    if (menu) menu.hidden = true;
    $("#novelExportBtn")?.setAttribute("aria-expanded", "false");
    const kind = btn.dataset.novelExport;
    if (kind === "md-extras") void exportBook("md", true);
    else void exportBook(kind === "md" ? "md" : "txt", false);
  });
  $("#novelBooksBtn")?.addEventListener("click", () => {
    const dialog = $("#novelBooksDialog");
    if (!dialog) return;
    void renderBookDialog();
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  });
  $("#novelBooksClose")?.addEventListener("click", () => {
    const dialog = $("#novelBooksDialog");
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  });

  $("#novelChapterFilter")?.addEventListener("input", (event) => {
    novel.filter = String(event.target.value || "");
    renderChapters();
  });

  $("#novelChapterList")?.addEventListener("change", (event) => {
    const box = event.target.closest("[data-novel-pick]");
    if (!box) return;
    const no = Number(box.dataset.novelPick);
    if (box.checked) novel.picked.add(no);
    else novel.picked.delete(no);
    box.closest(".novel-chapter-row")?.classList.toggle("picked", box.checked);
    updatePickedCount();
    // 阶段二的素材就绪状态跟着勾选变，必须重渲染生成按钮。
    if (novel.stage !== "split") renderStagePanel();
  });

  $("#novelChapterList")?.addEventListener("click", (event) => {
    // 阶段二下章节行是 label（点行=勾选），不再切章节；拆章阶段才是打开单章。
    if (novel.stage !== "split") return;
    const btn = event.target.closest("[data-novel-chapter]");
    if (!btn) return;
    void openChapter(Number(btn.dataset.novelChapter));
  });

  // ---- 阶段二：阶段切换与生成 ----
  $("#novelStages")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-novel-stage]");
    if (!btn || btn.disabled) return;
    setStage(btn.dataset.novelStage);
  });
  $("#novelStageGenerate")?.addEventListener("click", () => void generateStage());
  $("#novelStageCancel")?.addEventListener("click", () => void cancelStageTask());
  $("#novelStageRetry")?.addEventListener("click", () => void retryStageTask());
  $("#novelStageApply")?.addEventListener("click", () => void applyStageResult());
  // 两级模型选择：换供应商要重新拉那一家的模型目录（与提词库同口径）。
  $("#novelStageProvider")?.addEventListener("change", async () => {
    novel.stageProviderKey = String($("#novelStageProvider")?.value || "");
    novel.stageModelName = "";
    await fillStageModelOptions();
    renderStagePanel();
  });
  $("#novelStageModel")?.addEventListener("change", () => {
    novel.stageModelName = String($("#novelStageModel")?.value || "");
    renderStagePanel();
  });

  // ---- 阶段三：剧本场次 ----
  $("#novelSceneList")?.addEventListener("click", (event) => {
    const action = event.target.closest("[data-novel-scene-action]");
    if (action) {
      const no = Number(action.dataset.novelSceneNo);
      const kind = action.dataset.novelSceneAction;
      if (kind === "delete") void deleteScene(no);
      else void moveScene(no, kind);
      return;
    }
    const open = event.target.closest("[data-novel-scene-open]");
    if (!open) return;
    const no = Number(open.dataset.novelSceneOpen);
    // 再点同一场就收起编辑区（不重复展开）。
    novel.sceneEditing = novel.sceneEditing === no ? 0 : no;
    renderSceneBlock(true);
  });
  $("#novelSceneSave")?.addEventListener("click", () => void saveScene());
  $("#novelSceneCancel")?.addEventListener("click", () => {
    novel.sceneEditing = 0;
    renderSceneBlock(true);
  });

  $("#novelBookList")?.addEventListener("click", (event) => {
    const del = event.target.closest("[data-novel-delete-book]");
    if (del) {
      void deleteBook(del.dataset.novelDeleteBook, del.dataset.novelDeleteTitle || "");
      return;
    }
    const open = event.target.closest("[data-novel-open]");
    if (!open) return;
    $("#novelBooksDialog")?.close?.();
    void openBook(open.dataset.novelOpen);
  });

  $("#novelPanes")?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-novel-pane]");
    if (!btn) return;
    setPane(btn.dataset.novelPane);
  });

  $("#novelChapterTitle")?.addEventListener("input", markDirty);
  $("#novelChapterBody")?.addEventListener("input", markDirty);

  // 横竖屏切换会改变三栏/分档布局，inert 必须跟着重算，否则转屏后整栏点不动。
  if (typeof window.matchMedia === "function") {
    const query = window.matchMedia(PORTRAIT_QUERY);
    const onChange = () => applyPaneAccessibility();
    if (typeof query.addEventListener === "function") query.addEventListener("change", onChange);
    else if (typeof query.addListener === "function") query.addListener(onChange);
  }

  // 功能页切换：进入本页时按服务端校准一次（页面刷新后也能恢复上次作品）。
  onStudioPageChange((next, detail) => {
    if (detail?.closed || next !== "novel") {
      // 切走就停轮询：页面隐藏后继续轮询只是白耗请求。
      stopTaskPolling();
      return;
    }
    // 模型下拉依赖 bootstrap 的供应商配置：首屏时它可能还没到，进页面时重算一次。
    fillStageModels();
    if (novel.bookId) void openBook(novel.bookId);
    else if (novel.stage !== "split") void refreshStageTasks();
  });

  setPane(novel.pane);
  fillStageModels();
  renderAll();
}
