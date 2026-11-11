// Body drafts are local recovery copies, separate from server versions and setup drafts.
// The backend is injectable so the same ordering rules run in Node and in IndexedDB.
// IndexedDB schema v1: key/module/bookId/chapterNo/sourceId/sourceLabel identify the
// source; baseRevision/baseContent/readOnly/sourceExists describe the baseline;
// content/editId/updatedAt hold the recovery copy and compare-and-delete token.
const BODY_DRAFT_DB = "naibaBodyDrafts";
const BODY_DRAFT_STORE = "drafts";

function draftKey(context) {
  const parts = [context.module, context.bookId, Number(context.chapterNo), String(context.sourceId || "current")];
  if (context.module === "novel") {
    // Keep a conflict copy separate even if an immutable source file was modified externally.
    const text = String(context.baseContent ?? "");
    let first = 2166136261, second = 5381;
    for (let i = 0; i < text.length; i += 1) {
      first = Math.imul(first ^ text.charCodeAt(i), 16777619);
      second = Math.imul(second, 33) ^ text.charCodeAt(i);
    }
    parts.push(`${text.length}:${first >>> 0}:${second >>> 0}`);
  }
  return JSON.stringify(parts);
}

export function createMemoryDraftBackend() {
  const rows = new Map();
  return {
    async put(row) { rows.set(row.key, { ...row }); },
    async get(key) { return rows.has(key) ? { ...rows.get(key) } : null; },
    async list() { return [...rows.values()].map((row) => ({ ...row })); },
    async remove(key, editId) {
      if (rows.get(key)?.editId !== editId) return false;
      rows.delete(key);
      return true;
    },
  };
}

function indexedDraftBackend() {
  let opening;
  function database() {
    if (!opening) opening = new Promise((resolve, reject) => {
      if (!globalThis.indexedDB) { reject(new Error("浏览器未开放本地正文存储")); return; }
      const request = globalThis.indexedDB.open(BODY_DRAFT_DB, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(BODY_DRAFT_STORE)) {
          request.result.createObjectStore(BODY_DRAFT_STORE, { keyPath: "key" });
        }
      };
      request.onerror = () => reject(request.error || new Error("无法打开正文草稿存储"));
      request.onblocked = () => reject(new Error("正文存储升级被其他窗口占用，请关闭旧窗口后重试"));
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => { db.close(); opening = null; };
        resolve(db);
      };
    }).catch((error) => { opening = null; throw error; });
    return opening;
  }
  async function transaction(mode, action) {
    const db = await database();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(BODY_DRAFT_STORE, mode);
      let value;
      tx.oncomplete = () => resolve(value);
      tx.onerror = tx.onabort = () => reject(tx.error || new Error("正文草稿写入失败"));
      try { action(tx.objectStore(BODY_DRAFT_STORE), (result) => { value = result; }); }
      catch (error) { tx.abort(); reject(error); }
    });
  }
  return {
    put: (row) => transaction("readwrite", (store) => store.put(row)),
    get: (key) => transaction("readonly", (store, done) => {
      store.get(key).onsuccess = (event) => done(event.target.result || null);
    }),
    list: () => transaction("readonly", (store, done) => {
      store.getAll().onsuccess = (event) => done(event.target.result || []);
    }),
    remove: (key, editId) => transaction("readwrite", (store, done) => {
      store.get(key).onsuccess = (event) => {
        if (event.target.result?.editId !== editId) { done(false); return; }
        store.delete(key);
        done(true);
      };
    }),
  };
}

export function bodyDraftMatches(row, context) {
  if (!row || !context || context.readOnly || context.sourceExists === false) return false;
  if (row.module !== context.module || row.bookId !== context.bookId
    || Number(row.chapterNo) !== Number(context.chapterNo)) return false;
  if (row.module === "writer") {
    return row.baseRevision != null && context.baseRevision != null
      && String(row.baseRevision) === String(context.baseRevision);
  }
  return String(row.sourceId) === String(context.sourceId)
    && typeof row.baseContent === "string" && row.baseContent === context.baseContent;
}

export function createBodyDraftSession({ backend = indexedDraftBackend(), onStatus = () => {} } = {}) {
  const pending = new Map();
  const failed = new Map();
  const latest = new Map();
  let serial = 0;
  let lastUpdate = 0;
  const instance = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  function enqueue(row) {
    onStatus(failed.size ? "error" : "saving", row);
    const before = pending.get(row.key) || Promise.resolve();
    const operation = before.catch(() => {}).then(() => backend.put(row)).then(() => {
      if (latest.get(row.key)?.editId === row.editId) {
        failed.delete(row.key);
        onStatus(failed.size ? "error" : pending.size > 1 ? "saving" : "saved", row);
      }
    }).catch((error) => {
      if (latest.get(row.key)?.editId === row.editId) {
        failed.set(row.key, row);
        onStatus("error", row, error);
      }
    }).finally(() => {
      if (pending.get(row.key) === operation) pending.delete(row.key);
    });
    pending.set(row.key, operation);
  }
  return {
    capture(context, content) {
      if (!context?.bookId || !context.chapterNo || context.readOnly) return null;
      const key = draftKey(context);
      const previous = latest.get(key);
      if (previous && previous.content === String(content)
        && previous.baseRevision === context.baseRevision && previous.baseContent === context.baseContent) return previous;
      lastUpdate = Math.max(Date.now(), lastUpdate + 1);
      const row = { ...context, key, content: String(content), editId: `${instance}-${++serial}`, updatedAt: lastUpdate };
      latest.set(key, row);
      enqueue(row);
      return row;
    },
    async flush() {
      for (const row of failed.values()) if (!pending.has(row.key)) enqueue(row);
      while (pending.size) await Promise.all([...pending.values()]);
      return failed.size === 0;
    },
    hasUnstored() { return Boolean(pending.size || failed.size); },
    async read(context) {
      await this.flush();
      return backend.get(draftKey(context));
    },
    async list(module, bookId, chapterNo) {
      await this.flush();
      return (await backend.list()).filter((row) => row.module === module && row.bookId === bookId
        && (chapterNo == null || Number(row.chapterNo) === Number(chapterNo)))
        .sort((a, b) => b.updatedAt - a.updatedAt);
    },
    async remove(row) {
      if (!row) return false;
      // A migrated baseline must be durable before deleting its only recovery copy.
      if (!await this.flush()) return false;
      // The database comparison is atomic; another tab or save-time edit cannot be erased.
      let removed;
      try { removed = await backend.remove(row.key, row.editId); }
      catch (error) { onStatus("error", row, error); return false; }
      if (removed && latest.get(row.key)?.editId === row.editId) latest.delete(row.key);
      return removed;
    },
  };
}

function draftStyles() {
  if (document.getElementById("bodyDraftStyles")) return;
  const style = document.createElement("style");
  style.id = "bodyDraftStyles";
  style.textContent = `.body-draft-note{font-size:12px;line-height:1.6;overflow-wrap:anywhere;margin:6px 0;color:var(--muted)}
    .body-draft-note[data-state=error]{color:var(--danger,#b33)}
    .body-draft-list{grid-column:1/-1;margin:8px 0;padding:10px;border:1px solid var(--border,#8885);border-radius:8px;min-width:0;overflow-wrap:anywhere}
    .body-draft-list summary{cursor:pointer}.body-draft-list button{margin:4px;max-width:100%;white-space:normal;overflow-wrap:anywhere}
    .body-draft-dialog{width:min(680px,calc(100vw - 32px));max-width:calc(100vw - 32px);box-sizing:border-box;border:1px solid var(--border,#8885);border-radius:12px;padding:18px;background:var(--surface,#fff);color:var(--text,#222)}
    .body-draft-dialog textarea{box-sizing:border-box;width:100%;min-height:200px;max-height:50vh;resize:vertical}
    .body-draft-dialog footer{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.body-draft-dialog p{overflow-wrap:anywhere}`;
  document.head.append(style);
}

export function createBodyDraftPanel({ module, host, statusHost, open }) {
  let listToken = 0;
  let note;
  function status(kind) {
    const parent = document.querySelector(statusHost);
    if (!parent) return;
    draftStyles();
    if (!note?.isConnected) {
      note = document.createElement("p");
      note.className = "body-draft-note";
      note.dataset.bodyDraftStatus = module;
      note.setAttribute("role", "status");
      parent.append(note);
    }
    note.dataset.state = kind;
    note.textContent = {
      saving: "正在暂存正文…",
      saved: "正文已暂存到当前浏览器；保存后才会写入作品版本。",
      restored: "已恢复本地正文草稿，尚未保存到作品。",
      error: "正文尚未暂存，请先保存或复制。",
      clean: "",
    }[kind] || kind;
  }
  return {
    status,
    async refresh(session, bookId) {
      const token = ++listToken;
      let rows;
      try { rows = bookId ? await session.list(module, bookId) : []; }
      catch (_error) { status("error"); return; }
      if (token !== listToken) return;
      const parent = document.querySelector(host);
      if (!parent) return;
      const old = parent.querySelector(`[data-body-draft-list="${module}"]`);
      old?.remove();
      if (!rows.length) return;
      draftStyles();
      const box = document.createElement("details");
      box.className = "body-draft-list";
      box.dataset.bodyDraftList = module;
      box.open = true;
      const summary = document.createElement("summary");
      summary.textContent = `本书有 ${rows.length} 份未保存的正文草稿`;
      const hint = document.createElement("p");
      hint.className = "body-draft-note";
      hint.textContent = "草稿只保存在当前浏览器地址。换端口、地址或设备不会同步；来源变化时可查看、复制或选择继续编辑。";
      box.append(summary, hint);
      for (const row of rows) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "control-button";
        button.textContent = `第 ${row.chapterNo} 章 · ${row.sourceLabel || "正文草稿"} · ${new Date(row.updatedAt).toLocaleString()}`;
        button.dataset.bodyDraftKey = row.key;
        button.addEventListener("click", () => void open(row));
        box.append(button);
      }
      const columns = parent.querySelector(module === "writer" ? "#writerColumns" : "#novelColumns");
      if (columns) columns.before(box);
      else parent.prepend(box);
    },
  };
}

export function chooseBodyDraft(row, { canContinue = true, reason = "正文的基础版本已变化，草稿仍完整保留。" } = {}) {
  draftStyles();
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "body-draft-dialog";
    dialog.dataset.bodyDraftConflict = row.module;
    const heading = document.createElement("h3");
    heading.textContent = `第 ${row.chapterNo} 章本地草稿`;
    const note = document.createElement("p");
    note.textContent = reason;
    const body = document.createElement("textarea");
    body.value = row.content;
    body.readOnly = true;
    body.setAttribute("aria-label", "本地正文草稿（可复制）");
    const footer = document.createElement("footer");
    const finish = (choice) => { dialog.close(); dialog.remove(); resolve(choice); };
    const actions = [["copy", "复制草稿"], ["continue", "基于当前版本继续编辑"], ["discard", "丢弃此草稿"], ["keep", "保留，稍后处理"]];
    for (const [action, label] of actions) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "control-button";
      button.textContent = label;
      button.dataset.bodyDraftAction = action;
      button.disabled = action === "continue" && !canContinue;
      button.addEventListener("click", async () => {
        if (action === "copy") {
          body.focus(); body.select();
          try { await navigator.clipboard.writeText(row.content); button.textContent = "已复制"; }
          catch (_error) { note.textContent = "已选中草稿全文，请复制选中的文字。"; }
        } else finish(action);
      });
      footer.append(button);
    }
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish("keep"); });
    dialog.append(heading, note, body, footer);
    document.body.append(dialog);
    dialog.showModal();
  });
}

export function bindBodyDraftLifecycle(session, capture) {
  const flush = () => { capture(); void session.flush(); };
  document.addEventListener("visibilitychange", () => { if (document.hidden) flush(); });
  window.addEventListener("pagehide", flush);
  window.addEventListener("beforeunload", (event) => {
    capture();
    if (session.hasUnstored()) { event.preventDefault(); event.returnValue = ""; }
  });
}
