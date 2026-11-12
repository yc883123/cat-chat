// 两个小说工作台共用的作品系统词编辑器。保存只更新独立提示词字段。
import { api, toast } from "./01-core.js";
import { bindWorkbenchDialogReturn } from "./22a-workbench-ui.js";

export function bindWorkSystemPrompt({ module, getBook, onSaved }) {
  const prefix = `${module}SystemPrompt`;
  const trigger = document.getElementById(`${prefix}Btn`);
  if (!trigger) return;
  const dialog = document.createElement("dialog");
  dialog.id = `${prefix}Dialog`;
  dialog.className = "writer-dialog work-system-prompt-dialog";
  dialog.setAttribute("aria-labelledby", `${prefix}Title`);
  // Only fixed application markup is interpolated here; book text goes through textContent/value.
  dialog.innerHTML = `<form class="writer-dialog-form">
    <h3 id="${prefix}Title">作品系统提示词</h3>
    <p class="writer-note" data-book-title></p>
    <p class="writer-note">用于本作品各个 AI 生成阶段，保存后对新发起的任务生效。</p>
    <label class="writer-field">系统提示词<textarea id="${prefix}Text" rows="12" maxlength="100000" spellcheck="false"></textarea></label>
    <p class="writer-dialog-error" id="${prefix}Error" role="alert"></p>
    <div class="writer-dialog-actions">
      <button class="control-button" id="${prefix}Clear" type="button">清空</button>
      <button class="control-button" id="${prefix}Cancel" type="button">取消</button>
      <button class="control-button" id="${prefix}Save" type="submit">保存</button>
    </div></form>`;
  document.body.append(dialog);
  const text = dialog.querySelector("textarea");
  const error = dialog.querySelector('[role="alert"]');
  const save = dialog.querySelector('[type="submit"]');
  const clear = dialog.querySelector(`#${prefix}Clear`);
  const drafts = new Map();
  let target = null;
  let generation = 0;
  let saving = false;
  let failedSave = false;
  const discard = () => {
    if (saving) return;
    if (target && !failedSave) drafts.delete(target.id);
    generation += 1;
    dialog.close();
  };
  dialog.querySelector(`#${prefix}Cancel`).addEventListener("click", discard);
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); discard(); });
  clear.addEventListener("click", () => {
    text.value = "";
    if (target) drafts.set(target.id, "");
    text.focus();
  });
  text.addEventListener("input", () => { if (target) drafts.set(target.id, text.value); });
  bindWorkbenchDialogReturn(dialog, document.getElementById(`${module}MoreBtn`));
  trigger.addEventListener("click", async () => {
    const book = getBook();
    if (!book?.id) { toast("先打开一部作品"); return; }
    const token = ++generation;
    failedSave = false;
    target = { id: book.id, revision: 0 };
    dialog.querySelector("[data-book-title]").textContent = book.title || "未命名作品";
    text.value = drafts.has(book.id) ? drafts.get(book.id) : String(book.global_system_prompt || "");
    error.textContent = "正在读取作品设置…";
    save.disabled = true;
    clear.disabled = true;
    text.disabled = true;
    if (!dialog.open) dialog.showModal();
    try {
      const payload = await api(`/api/${module}/books/${encodeURIComponent(book.id)}`);
      if (token !== generation || getBook()?.id !== book.id || !dialog.open) return;
      target.revision = Number(payload.book?.global_system_prompt_revision || 0);
      text.value = drafts.has(book.id) ? drafts.get(book.id) : String(payload.book?.global_system_prompt || "");
      error.textContent = drafts.has(book.id) ? "已保留上次未保存的草稿；保存后覆盖当前作品设置。" : "";
      text.disabled = false;
      clear.disabled = false;
      save.disabled = false;
      text.focus();
    } catch (failure) {
      if (token === generation) error.textContent = String(failure?.message || failure);
    }
  });
  dialog.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving || save.disabled || !target) return;
    if (getBook()?.id !== target.id) { error.textContent = "作品已切换，请取消后重新打开。"; return; }
    const { id, revision } = target;
    const value = text.value;
    const token = generation;
    drafts.set(id, value);
    saving = true;
    save.disabled = true;
    error.textContent = "正在保存…";
    try {
      const payload = await api(`/api/${module}/books/${encodeURIComponent(id)}/system-prompt`, {
        method: "PATCH", body: { global_system_prompt: value, global_system_prompt_revision: revision },
      });
      if (getBook()?.id === id) onSaved(payload.book);
      if (token !== generation) return;
      target.revision = Number(payload.book?.global_system_prompt_revision || 0);
      failedSave = false;
      if (text.value === value) {
        drafts.delete(id);
        dialog.close();
        toast("作品系统提示词已保存，新任务将使用这份设置");
      } else error.textContent = "已保存提交时的内容，框内新增修改尚未保存。";
    } catch (failure) {
      if (token === generation) {
        failedSave = true;
        error.textContent = `${String(failure?.message || failure)}（草稿保留，关闭后重新打开可继续编辑）`;
      }
    } finally {
      saving = false;
      if (token === generation) save.disabled = false;
    }
  });
}
