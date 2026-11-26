// ============================================================
// 33-genui.js —— GenUI（移植自 omdsh-dev/dsh-genui，MIT 协议）前端桥接
// ============================================================
// 职责：
// 1. 懒加载 /vendor/genui/cat-genui.js：不含 dsh-ui 围栏的会话零额外下载开销。
// 2. 通过 MutationObserver 监听 #messages 与 #auxChatMessages 中的 .genui-slot：
//    - 流式期间只显示轻量占位条，终态助手正文的闭合围栏才挂载完整卡片；
//    - 节点移出 DOM 时自动 unmount React root，零内存泄漏。
// 3. 卡片动作只发回卡片所属会话，不读写主对话或辅助对话的输入框草稿。

import { $, toast } from "./01-core.js";
import { conversationOpenGeneration, isConversationOpenCurrent } from "./08-conversations.js";
import { sendGenuiChatAction } from "./11-run-stream.js";
import { auxChatState, sendGenuiAuxAction } from "./21-aux-chat.js";

const GENUI_RUNTIME_URL = "/vendor/genui/cat-genui.js";
const GENUI_ASSET_BASE = "/vendor/genui/assets/";

let runtimePromise = null;
let observerBound = false;
const mountedInstances = new WeakMap();
const pendingSlots = new WeakSet();

function loadGenuiRuntime() {
  if (typeof window === "undefined") return Promise.reject(new Error("no window"));
  if (window.__CatGenui__) return Promise.resolve(window.__CatGenui__);
  if (runtimePromise) return runtimePromise;
  runtimePromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = GENUI_RUNTIME_URL;
    script.async = true;
    script.dataset.catGenuiRuntime = "1";
    script.onload = () => {
      if (!window.__CatGenui__) {
        script.remove();
        runtimePromise = null;
        reject(new Error("CatGenui runtime loaded without global export"));
        return;
      }
      window.__CatGenui__.setAssetBase(GENUI_ASSET_BASE);
      window.__CatGenui__.setLocale("zh-CN");
      resolve(window.__CatGenui__);
    };
    script.onerror = () => {
      script.remove();
      runtimePromise = null;
      reject(new Error("Failed to load GenUI runtime"));
    };
    document.head.appendChild(script);
  });
  return runtimePromise;
}

function formatGenuiActionMessage(action, payload) {
  const act = typeof action === "string" ? action.trim() : "";
  if (!act) return "";
  const hasPayload = payload && typeof payload === "object" && Object.keys(payload).length > 0;
  return hasPayload
    ? `[genui-action] ${act}\n${JSON.stringify(payload)}`
    : `[genui-action] ${act}`;
}

function sourceForSlot(slot) {
  if (!slot.isConnected || slot.dataset.genuiStreaming === "1") return null;
  const row = slot.closest(".message-row.assistant[data-genui-conversation-id]");
  const conversationId = row?.dataset.genuiConversationId;
  if (!conversationId) return null;
  if (slot.closest("#auxChatMessages")) {
    return auxChatState.activeId === conversationId ? { row, conversationId, auxiliary: true } : null;
  }
  const generation = conversationOpenGeneration();
  if (!slot.closest("#messages") || !row.dataset.genuiMessageId
      || !isConversationOpenCurrent(generation, conversationId)) return null;
  return { row, conversationId, auxiliary: false, generation };
}

function sameSlotSource(left, right) {
  return left?.row === right.row && left.conversationId === right.conversationId
    && left.auxiliary === right.auxiliary && left.generation === right.generation;
}

function handleGenuiAction(slot, mount, action, payload) {
  const source = mount.source;
  const current = sourceForSlot(slot);
  // 卡片组件可能延迟上报滑块/表单操作。离开会话、换标签或移除卡片后丢弃旧回调。
  if (mountedInstances.get(slot) !== mount || !sameSlotSource(current, source)
      || !slot.getClientRects().length) return;
  const text = formatGenuiActionMessage(action, payload);
  if (!text) return;
  const send = source.auxiliary ? sendGenuiAuxAction : sendGenuiChatAction;
  void send(text, source.conversationId).catch((error) => toast(`卡片操作发送失败：${error.message}`));
}

function computeSlotStateKey(slot, raw, source) {
  // 辅助标签关闭即清理内容；不把其中的表单值落到浏览器持久存储。
  if (source.auxiliary) return undefined;
  const { row, conversationId } = source;
  const siblings = [...row.querySelectorAll("[data-genui-fence]")];
  const idx = Math.max(0, siblings.indexOf(slot));
  let hash = 2166136261;
  for (let i = 0; i < raw.length; i += 1) {
    hash ^= raw.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return `${conversationId}:${row.dataset.genuiMessageId}:${idx}:${(hash >>> 0).toString(36)}`;
}

function applyMountResult(slot, result) {
  const diag = slot.querySelector(".genui-diagnostic");
  if (result.rendered) {
    slot.classList.add("is-rendered");
    slot.classList.remove("is-failed");
    if (diag) {
      diag.hidden = true;
      diag.textContent = "";
    }
    return;
  }
  slot.classList.remove("is-rendered");
  if (result.failure) {
    slot.classList.add("is-failed");
    if (diag) {
      diag.textContent = result.failure;
      diag.hidden = false;
    }
  }
}

export async function hydrateGenuiSlot(slot) {
  const source = slot && sourceForSlot(slot);
  if (!source) return;
  const rawEl = slot.querySelector(".genui-raw");
  const mountEl = slot.querySelector(".genui-mount");
  if (!rawEl || !mountEl) return;
  const raw = String(rawEl.value ?? rawEl.textContent ?? "");
  if (!raw.trim()) return;

  const existing = mountedInstances.get(slot);
  if (existing?.raw === raw && sameSlotSource(source, existing.source)) return;
  if (pendingSlots.has(slot)) return;
  pendingSlots.add(slot);
  try {
    const runtime = await loadGenuiRuntime();
    const current = sourceForSlot(slot);
    if (!sameSlotSource(current, source)) return;
    const mount = { source, raw, instance: null };
    const opts = {
      raw,
      settled: true,
      exportEnabled: true,
      locale: "zh-CN",
      stateKey: computeSlotStateKey(slot, raw, source),
      onAction: (action, payload) => handleGenuiAction(slot, mount, action, payload),
    };
    const previous = mountedInstances.get(slot);
    mount.instance = previous?.instance || runtime.mount(mountEl, opts);
    const result = previous ? mount.instance.update(opts) : mount.instance;
    mountedInstances.set(slot, mount);
    applyMountResult(slot, result);
  } catch (error) {
    console.warn("[cat-genui] render failed:", error);
    applyMountResult(slot, { failure: "互动卡片加载失败，已显示原始内容。刷新后可重试。" });
  } finally {
    pendingSlots.delete(slot);
  }
}

export function unmountGenuiIn(node) {
  if (!node || node.nodeType !== 1) return;
  const slots = [];
  if (node.matches?.("[data-genui-fence]")) slots.push(node);
  node.querySelectorAll?.("[data-genui-fence]").forEach((el) => slots.push(el));
  slots.forEach((slot) => {
    const mount = mountedInstances.get(slot);
    if (mount) {
      try { mount.instance.unmount(); } catch (_) { /* 忽略已销毁的挂载点 */ }
      mountedInstances.delete(slot);
    }
  });
}

export function hydrateGenuiIn(root) {
  if (!root || root.nodeType !== 1) return;
  if (root.matches?.("[data-genui-fence]")) void hydrateGenuiSlot(root);
  root.querySelectorAll?.("[data-genui-fence]").forEach((slot) => {
    void hydrateGenuiSlot(slot);
  });
}

function alignVendorActionStatus(node) {
  // 引擎在回调发出时便把 choices 标成「已发送」。网络是否受理由宿主判断，
  // 因此只校正引擎自己的状态文案，不替换 React 管理的文本节点或可见选项。
  if (node.nodeType === 3) {
    if (node.data === "已发送 · 仍可修改" && node.parentElement?.closest(".genui-mount [role='status']")) {
      node.data = "已触发 · 仍可修改";
    }
    return;
  }
  if (node.nodeType !== 1 || !node.closest(".genui-mount")) return;
  if (node.matches("[role='status']") && node.firstChild) alignVendorActionStatus(node.firstChild);
  node.querySelectorAll("[role='status']").forEach((status) => {
    if (status.firstChild) alignVendorActionStatus(status.firstChild);
  });
}

export function bindGenuiObserver() {
  if (observerBound || typeof MutationObserver !== "function") return;
  observerBound = true;
  const targets = [$("#messages"), $("#auxChatMessages")].filter(Boolean);
  if (!targets.length) return;
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === "characterData") {
        alignVendorActionStatus(mutation.target);
        continue;
      }
      mutation.removedNodes.forEach((node) => unmountGenuiIn(node));
      mutation.addedNodes.forEach((node) => {
        hydrateGenuiIn(node);
        alignVendorActionStatus(node);
      });
    }
  });
  targets.forEach((target) => {
    observer.observe(target, { childList: true, characterData: true, subtree: true });
    hydrateGenuiIn(target);
  });
}
