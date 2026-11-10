// 从拆书匠明确选稿并创建续写作品；创建完成后交回织书台既有写作流程。
import { $, api, toast } from "./01-core.js";

const STEPS = ["选作品", "选稿件", "选末章", "确认现状", "建新章"];
const SUMMARY_FIELDS = [
  ["characters_state", "人物现状"], ["relations_state", "关系现状"],
  ["plot_state", "剧情现状"], ["next_outline", "后续大纲"],
];

function element(tag, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

export function bindWriterNovelContinuation({ onImported, beforeOpen = () => true }) {
  const dialog = $("#writerNovelImportDialog");
  if (!dialog || dialog.dataset.bound) return;
  dialog.dataset.bound = "1";
  const body = $("#writerNovelImportBody");
  const errorBox = $("#writerNovelImportError");
  const next = $("#writerNovelImportNext");
  const back = $("#writerNovelImportBack");
  const close = $("#writerNovelImportClose");
  let draft = null, epoch = 0, busy = false, opening = false;

  function requestKey() {
    return `novel_${globalThis.crypto?.randomUUID?.() || `${Date.now()}_${Math.random().toString(36).slice(2)}_${Math.random().toString(36).slice(2)}`}`;
  }

  function showError(error) {
    const payload = error?.payload || error || {};
    const issues = Array.isArray(payload.issues) ? payload.issues : [];
    errorBox.textContent = [String(payload.error || error?.message || error),
      ...issues.map(row => `第 ${row.chapter_no} 章：${row.reason}`)].join("\n");
  }

  function selection() {
    return { book_id: draft.bookId, source_kind: draft.sourceKind,
      end_chapter_no: draft.endNo, manuscripts_by_chapter: { ...draft.manuscripts } };
  }

  function render() {
    body.replaceChildren();
    errorBox.textContent = "";
    $("#writerNovelImportStep").textContent = `第 ${draft.step} / 5 步 · ${STEPS[draft.step - 1]}`;
    back.hidden = draft.step === 1;
    next.textContent = draft.step === 5 ? "创建续写作品" : "下一步";
    if (draft.step === 1) {
      body.append(element("p", "选择拆书匠作品。导入会创建新作品，保留原作与全部改编稿。", "writer-note"));
      const label = element("label", "作品", "writer-field");
      const select = element("select");
      select.id = "writerNovelSourceBook";
      select.append(new Option("请选择作品", ""), ...draft.books.map(book => new Option(book.title || book.id, book.id)));
      select.value = draft.bookId;
      select.addEventListener("change", () => {
        draft.bookId = select.value; draft.sources = []; draft.sourceKind = "";
        draft.manuscripts = {}; draft.preview = null; draft.summary = {};
        draft.title = ""; draft.confirmed = false;
      });
      label.append(select); body.append(label);
    } else if (draft.step === 2) {
      body.append(element("p", "必须明确选一种来源。原文与改编稿可能已分叉；改编稿需要逐章指定，不会自动挑最新稿。", "writer-note"));
      for (const [value, text] of [["original", "原文"], ["manuscript", "指定改编稿（逐章选择）"]]) {
        const label = element("label", "", "writer-field writer-novel-source-choice");
        const input = document.createElement("input");
        input.type = "radio"; input.name = "writerNovelSourceKind";
        input.value = value; input.checked = draft.sourceKind === value;
        input.addEventListener("change", () => { draft.sourceKind = value; draft.preview = null; draft.manuscripts = {}; });
        label.append(input, element("span", text)); body.append(label);
      }
    } else if (draft.step === 3) {
      const label = element("label", "接在哪一章之后", "writer-field");
      const select = element("select"); select.id = "writerNovelEndChapter";
      select.append(...draft.sources.map(row => new Option(`第 ${row.no} 章 ${row.title || ""}`, String(row.no))));
      select.value = String(draft.endNo);
      select.addEventListener("change", () => { draft.endNo = Number(select.value); draft.preview = null; render(); });
      label.append(select); body.append(label);
      body.append(element("p", `导入截止第 ${draft.endNo} 章，新章将是第 ${draft.endNo + 1} 章。任何缺失、损坏或未选稿的章节都会阻止导入。`, "writer-note"));
      const list = element("div", "", "writer-novel-source-list");
      for (const row of draft.sources.filter(row => Number(row.no) <= draft.endNo)) {
        const field = element("label", `第 ${row.no} 章 ${row.title || ""}`, "writer-field");
        if (draft.sourceKind === "manuscript") {
          const pick = element("select"); pick.dataset.novelSourceChapter = String(row.no);
          pick.append(new Option("请选择这一章的改编稿", ""));
          for (const manuscript of row.manuscripts || []) {
            if (!["manual", "ai"].includes(manuscript.kind)) continue;
            const option = new Option(`${manuscript.label || manuscript.id}${manuscript.broken ? " · 已损坏" : ""}`, manuscript.id);
            option.disabled = Boolean(manuscript.broken); pick.append(option);
          }
          pick.value = draft.manuscripts[row.no] || "";
          pick.addEventListener("change", () => { draft.manuscripts[row.no] = pick.value; draft.preview = null; });
          field.append(pick);
        } else {
          const original = (row.manuscripts || []).find(item => item.kind === "original");
          field.append(element("small", original ? (original.broken ? "原文损坏，将阻止导入" : "原文") : "没有原文，将阻止导入"));
        }
        list.append(field);
      }
      body.append(list);
    } else if (draft.step === 4) {
      body.append(element("p", "请确认并填写四项内容，作为新章的写作依据。系统不会自动重构全书设定或判断伏笔是否已回收。", "writer-note"));
      const last = draft.preview?.last_chapter;
      if (last) {
        body.append(element("strong", `接续点：第 ${last.no} 章 ${last.title || ""}`));
        body.append(element("p", `末章共 ${last.total_chars} 字${last.truncated ? "，以下显示末尾节选" : ""}。请核对是否为这次要接续的稿件。`, "writer-note"));
        body.append(element("pre", last.excerpt || "", "writer-stage-result"));
      }
      for (const [key, text] of SUMMARY_FIELDS) {
        const label = element("label", text, "writer-field");
        const input = document.createElement("textarea"); input.rows = 3; input.maxLength = 2000;
        input.dataset.continuationSummary = key; input.value = draft.summary[key] || "";
        input.addEventListener("input", () => { draft.summary[key] = input.value; });
        label.append(input); body.append(label);
      }
      body.append(element("p", coverageText(draft.preview?.coverage), "writer-note"));
    } else {
      const label = element("label", "新作品名称", "writer-field");
      const input = document.createElement("input"); input.id = "writerNovelNewTitle";
      input.maxLength = 120; input.value = draft.title;
      input.addEventListener("input", () => { draft.title = input.value; });
      label.append(input); body.append(label);
      body.append(element("p", `将导入 ${draft.preview.chapter_count} 章只读正文，并在第 ${draft.endNo} 章之后创建第 ${draft.preview.new_chapter_no} 章。创建后由你点击生成；生成失败可在同一作品重试。`, "writer-note"));
      const labelConfirm = element("label", "", "writer-field writer-novel-source-choice");
      const check = document.createElement("input"); check.type = "checkbox";
      check.id = "writerNovelImportConfirmed"; check.checked = draft.confirmed;
      check.addEventListener("change", () => { draft.confirmed = check.checked; });
      labelConfirm.append(check, element("span", "我已确认稿件来源、续写落点和四项写作依据"));
      body.append(labelConfirm);
    }
  }

  function coverageText(coverage) {
    if (!coverage) return "导入检查只提供覆盖提示；是否足以支撑续写，请人工核对。";
    const relations = coverage.relations == null ? "关系资料未提供结构化记录" : `关系资料 ${coverage.relations} 条`;
    const comparison = coverage.divergent_chapters == null ? "与原文的差异尚无法完整核对"
      : `与原文内容不同的章节 ${coverage.divergent_chapters} 章`;
    return `导入 ${coverage.imported_chapters} 章；角色资料 ${coverage.characters} 人，末 ${coverage.recent_chapters} 章出现 ${coverage.recent_characters} 人；${relations}；${comparison}。剧情状态和伏笔仍需人工确认。${coverage.note || ""}`;
  }

  async function perform(action) {
    if (busy) return;
    busy = true;
    const token = epoch;
    for (const node of dialog.querySelectorAll("button, input, select, textarea")) node.disabled = true;
    errorBox.textContent = "";
    try { await action(token); }
    catch (error) { if (token === epoch) showError(error); }
    finally {
      busy = false;
      if (token === epoch) for (const node of dialog.querySelectorAll("button, input, select, textarea")) node.disabled = false;
    }
  }

  async function advance(token) {
    if (draft.step === 1) {
      if (!draft.bookId) throw new Error("先选择拆书匠作品");
      const payload = await api("/api/writer/novel-import/preview", { method: "POST", body: { book_id: draft.bookId } });
      if (token !== epoch) return;
      draft.sources = payload.chapter_sources || [];
      draft.endNo = Number(payload.end_chapter_no || draft.sources.at(-1)?.no || 0);
      if (!draft.sources.length) throw new Error("作品中没有可导入的章节");
      draft.title ||= `${draft.books.find(book => book.id === draft.bookId)?.title || "小说"} · 续写`;
    } else if (draft.step === 2) {
      if (!draft.sourceKind) throw new Error("请明确选择原文或指定改编稿");
    } else if (draft.step === 3) {
      const payload = await api("/api/writer/novel-import/preview", { method: "POST", body: selection() });
      if (token !== epoch) return;
      if (!payload.ready) throw Object.assign(new Error(payload.error || "导入预检未通过"), { payload });
      draft.preview = payload;
    } else if (draft.step === 4) {
      for (const [key, text] of SUMMARY_FIELDS) if (!String(draft.summary[key] || "").trim()) throw new Error(`请填写${text}`);
    } else {
      if (!draft.confirmed) throw new Error("请先确认稿件来源、续写落点和写作依据");
      if (draft.title.trim().length > 120) throw new Error("续写作品名称最多 120 字符，请手动缩短；原名称已保留");
      const submission = {
        ...selection(), summary: { ...draft.summary }, confirmed: true,
        title: draft.title, fingerprint: draft.preview.fingerprint,
      };
      const identity = JSON.stringify(submission);
      if (draft.submissionIdentity !== identity) {
        draft.requestKey = requestKey(); draft.submissionIdentity = identity;
      }
      const payload = await api("/api/writer/novel-import", { method: "POST",
        body: { ...submission, request_key: draft.requestKey } });
      if (token !== epoch) return;
      await onImported(payload);
      if (token !== epoch) return;
      dialog.close(); toast(`已创建续写作品，第 ${payload.new_chapter_no} 章等待生成`);
      return;
    }
    draft.step += 1; render();
  }

  next.addEventListener("click", () => void perform(advance));
  back.addEventListener("click", () => { if (!busy && draft.step > 1) { draft.step -= 1; draft.confirmed = false; render(); } });
  close.addEventListener("click", () => { if (!busy) dialog.close(); });
  for (const eventName of ["input", "change"]) {
    dialog.addEventListener(eventName, () => { if (!busy) errorBox.textContent = ""; });
  }
  dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); });
  dialog.addEventListener("close", () => { epoch += 1; });
  $("#writerNovelContinueBtn")?.addEventListener("click", async () => {
    if (busy || opening || dialog.open) return;
    opening = true;
    try {
      if (!await beforeOpen()) return;
      epoch += 1;
      draft = { step: 1, bookId: "", sourceKind: "", sources: [], books: [], endNo: 0,
        manuscripts: {}, preview: null, summary: {}, title: "", confirmed: false, requestKey: "" };
      render(); dialog.showModal();
      await perform(async token => {
        const payload = await api("/api/novel/books");
        if (token !== epoch) return;
        draft.books = payload.books || []; render();
        if (!draft.books.length) errorBox.textContent = "拆书匠还没有作品，请先导入小说。";
      });
    } catch (error) { toast(`打开续写向导失败：${error?.message || error}`); }
    finally { opening = false; }
  });
}
