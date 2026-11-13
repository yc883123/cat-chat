// Task TXT state is authoritative; live deltas are never proof of persistence.
import { api, state, toast } from "./01-core.js";

const controls = new WeakMap();
const TERMINAL = new Set(["done", "failed", "cancelled", "interrupted"]);

export function hasResultArtifact(task) {
  return Boolean(task && ["output_contract_version", "logical_generation_id", "result_artifact"]
    .some(key => Object.hasOwn(task, key) || Object.hasOwn(task.input || {}, key)));
}

export function resultArtifactReady(task) {
  return !hasResultArtifact(task) || (task.status === "done" && task.result_artifact?.status === "final");
}

export function resultArtifactText(task, live = "") {
  if (!hasResultArtifact(task)) return String(task?.result || task?.result_partial || live || "");
  if (task.result_artifact?.status === "invalid") return "";
  return String(task.result_artifact?.status === "final" ? task.result || ""
    : task.result_partial || (!TERMINAL.has(task.status) ? live : "") || "");
}

function preview(text, taskId, partial) {
  const dialog = document.createElement("dialog");
  dialog.className = "wb-result-preview";
  const title = document.createElement("strong");
  title.textContent = partial ? "TXT · 已确认的未完成内容" : "TXT · 完整结果";
  const body = document.createElement("pre");
  body.textContent = text;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "control-button";
  close.textContent = "关闭";
  close.addEventListener("click", () => dialog.close());
  dialog.append(title, body, close);
  dialog.setAttribute("aria-label", `${taskId} TXT 预览`);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  document.body.append(dialog);
  dialog.showModal();
  close.focus();
}

export function renderResultArtifact(anchor, task, { scope = "", isCurrent = () => true, onResume = null } = {}) {
  if (!anchor) return;
  let bar = anchor.previousElementSibling;
  if (!bar?.classList.contains("wb-result-artifact")) {
    bar = document.createElement("div");
    bar.className = "wb-result-artifact";
    anchor.before(bar);
  }
  let control = controls.get(bar);
  const identity = `${scope}:${task?.id || ""}`;
  if (!control || control.identity !== identity) {
    control = { identity, generation: (control?.generation || 0) + 1, busy: false,
      requestKey: globalThis.crypto?.randomUUID?.() || `txt-${Date.now()}-${Math.random().toString(36).slice(2)}` };
    controls.set(bar, control);
  }
  bar.hidden = !hasResultArtifact(task);
  if (bar.hidden) return;
  const artifact = task.result_artifact || { status: "invalid", error: "TXT 文件摘要缺失" };
  const note = document.createElement("span");
  note.setAttribute("role", "status");
  const label = { final: "TXT 已完成", partial: "TXT 未完成", invalid: "TXT 不可用" }[artifact.status] || "TXT 状态未知";
  note.textContent = `${label}${Number.isFinite(artifact.chars) ? ` · 已落盘 ${artifact.chars} 字符` : ""}`
    + (artifact.error ? ` · ${artifact.error}` : "");
  const nodes = [note];
  const current = () => controls.get(bar) === control && bar.isConnected && isCurrent();
  const action = (label, handler) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "control-button tiny";
    button.textContent = label;
    button.disabled = control.busy;
    button.addEventListener("click", async () => {
      if (control.busy || !current()) return;
      control.busy = true;
      bar.querySelectorAll("button").forEach(node => { node.disabled = true; });
      try { await handler(current, control.requestKey); }
      catch (error) { if (current()) toast(`TXT 操作失败：${error.message}`); }
      finally {
        control.busy = false;
        if (current()) bar.querySelectorAll("button").forEach(node => { node.disabled = false; });
      }
    });
    nodes.push(button);
  };
  if (artifact.status !== "invalid") {
    action("查看 TXT", async current => {
      const payload = await api(`/api/studio/tasks/${encodeURIComponent(task.id)}/artifact`);
      if (current() && payload.task_id === task.id) preview(payload.text, task.id, payload.artifact.status !== "final");
    });
    action(artifact.status === "final" ? "下载 TXT" : "下载未完成 TXT", async current => {
      const headers = state.token ? { Authorization: `Bearer ${state.token}` } : {};
      const response = await fetch(`/api/studio/tasks/${encodeURIComponent(task.id)}/artifact?download=1`, { headers });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      const blob = await response.blob();
      if (!current()) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${task.id}${artifact.status === "final" ? "" : "-partial"}.txt`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    if (onResume && ["failed", "cancelled", "interrupted"].includes(task.status)) {
      action(artifact.status === "final" ? "校验并恢复完成" : "继续未完成内容", async (current, requestKey) => {
        await onResume(task.id, requestKey, current);
      });
    }
  }
  bar.replaceChildren(...nodes);
}
