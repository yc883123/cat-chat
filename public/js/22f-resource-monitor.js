// 工作台共用的宿主资源监控；独立于各生成模块的任务轮询。
const POLL_MS = 3000;
const STALE_MS = 10000;
const REQUEST_TIMEOUT_MS = 8000;

function metric(value, available) {
  if (!available || !value || !Number.isFinite(value.total_bytes) || value.total_bytes <= 0
      || !Number.isFinite(value.used_bytes) || value.used_bytes < 0 || value.used_bytes > value.total_bytes
      || !Number.isFinite(value.percent) || value.percent < 0 || value.percent > 100) return null;
  return value;
}

function gib(bytes) { return (bytes / 1073741824).toFixed(1); }
function percent(value) { return `${Math.round(value.percent)}%`; }
function usage(value) { return `${gib(value.used_bytes)} / ${gib(value.total_bytes)} GiB · ${percent(value)}`; }
function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }
function setBar(node, value) {
  if (!node) return;
  node.hidden = !value;
  if (value) { node.value = value.percent; node.setAttribute("aria-valuetext", usage(value)); }
  else { node.removeAttribute("value"); node.removeAttribute("aria-valuetext"); }
}
function reasonText(code, gpu = false) {
  return ({ no_gpu: "未检测到可读取显存的显卡", no_gpus: "未检测到可读取显存的显卡",
    unsupported: "当前主机暂不支持读取这项资源", timeout: "读取超时，请稍后再试",
  })[code] || (gpu ? "显存暂不可用，无法读取主机显卡数据" : "内存暂不可用，无法读取主机数据");
}

// 由 Studio 组合根注入已鉴权的 api，避免反向依赖或另造请求凭据。
export function createResourceMonitor({ root, dialog, request, now = () => Date.now(),
  timers = globalThis, document: doc = globalThis.document, Observer = globalThis.MutationObserver } = {}) {
  if (!root || !dialog || typeof request !== "function") return null;
  const monitor = doc.getElementById("workbenchResourceMonitor");
  const ram = monitor?.querySelector('[data-resource-summary="memory"]');
  const gpu = monitor?.querySelector('[data-resource-summary="gpu"]');
  const status = doc.getElementById("resourceMonitorStatus");
  const memoryValue = doc.getElementById("resourceMemoryValue");
  const memoryReason = doc.getElementById("resourceMemoryReason");
  const gpuReason = doc.getElementById("resourceGpuReason");
  const gpuList = doc.getElementById("resourceGpuList");
  const sample = doc.getElementById("resourceMonitorSample");
  let running = false, disposed = false, epoch = 0, interval = null, expiry = null;
  let flight = null, snapshot = null, receivedAt = 0, unavailable = "资源数据尚未读取", opener = null;
  const gpuRows = new Map();
  const visible = () => !disposed && !root.hidden && !doc.hidden && !root.closest("[hidden]");
  const fresh = () => snapshot && now() - receivedAt <= STALE_MS;
  const clearExpiry = () => { if (expiry !== null) timers.clearTimeout(expiry); expiry = null; };
  const render = () => {
    const data = fresh() ? snapshot : null;
    const memory = metric(data?.memory, data?.memory?.available === true);
    const cards = data?.gpu_available === true && Array.isArray(data.gpus)
      ? data.gpus.map((card, order) => ({ card, order, value: metric(card, true) })) : [];
    const validCards = cards.filter(({ value }) => value);
    const updateSummary = (node, value) => {
      setText(node?.querySelector("[data-resource-full]"), value ? `${gib(value.used_bytes)}/${gib(value.total_bytes)} GiB` : "不可用");
      setText(node?.querySelector("[data-resource-compact]"), value ? percent(value) : "不可用");
    };
    updateSummary(ram, memory);
    // 多卡摘要显示最高占用；详情逐卡展示，避免把异构显卡相加伪装成一张卡。
    const busiest = validCards.reduce((best, { value }) => !best || value.percent > best.percent ? value : best, null);
    updateSummary(gpu, busiest);
    setBar(monitor?.querySelector('[data-resource-bar="memory"]'), memory);
    setBar(monitor?.querySelector('[data-resource-bar="gpu"]'), busiest);
    setBar(doc.getElementById("resourceMemoryBar"), memory);
    setText(status, data ? (memory || validCards.length ? "每 3 秒刷新" : "资源暂不可用") : unavailable);
    setText(memoryValue, memory ? usage(memory) : "不可用");
    setText(memoryReason, memory ? "" : data ? reasonText(data.memory?.reason || data.reason) : unavailable);
    setText(gpuReason, validCards.length ? (cards.length > 1 ? "页头显示占用最高的显卡" : "")
      : data ? reasonText(data.reason, true) : unavailable);
    const keep = new Set();
    for (const { card, order, value } of cards) {
      const key = `${card.index ?? order}:${order}`;
      keep.add(key);
      let row = gpuRows.get(key);
      if (!row) {
        const element = doc.createElement("div"); element.className = "resource-monitor-gpu";
        const name = doc.createElement("strong"), number = doc.createElement("p"), bar = doc.createElement("progress");
        bar.max = 100; bar.setAttribute("aria-label", `GPU ${card.index ?? order} 显存占用`);
        element.append(name, number, bar); gpuList?.append(element);
        row = { element, name, number, bar }; gpuRows.set(key, row);
      }
      setText(row.name, `GPU ${card.index ?? order} · ${String(card.name || "显卡")}`);
      setText(row.number, value ? usage(value) : "显存不可用");
      setBar(row.bar, value);
    }
    for (const [key, row] of gpuRows) if (!keep.has(key)) { row.element.remove(); gpuRows.delete(key); }
    setText(sample, data ? `采样时间 ${new Date(data.sampled_at).toLocaleTimeString()}` : "尚无有效采样");
  };
  const markUnavailable = (message) => { snapshot = null; unavailable = message; clearExpiry(); render(); };
  const refresh = () => {
    if (!running || !visible()) return Promise.resolve();
    if (flight) return flight.promise;
    const generation = epoch;
    const controller = new AbortController();
    const entry = { controller, promise: null, timeout: null, timedOut: false };
    flight = entry;
    entry.timeout = timers.setTimeout(() => {
      entry.timedOut = true; controller.abort();
      if (running && epoch === generation) markUnavailable("资源读取超时，暂不可用");
    }, REQUEST_TIMEOUT_MS);
    entry.promise = (async () => {
      try {
        const data = await request("/api/system/resources", { signal: controller.signal, cache: "no-store" });
        if (disposed || !running || !visible() || generation !== epoch || entry.timedOut) return;
        if (data?.source !== "host" || !Number.isFinite(data.sampled_at) || data.sampled_at <= 0) {
          markUnavailable("资源数据无效，暂不可用"); return;
        }
        // 手机与主机可能有时钟差；TTL 从本地接收时刻计，服务端采样时间只用于展示。
        snapshot = data; receivedAt = now();
        clearExpiry(); render();
        expiry = timers.setTimeout(() => markUnavailable("资源数据已过期，暂不可用"), STALE_MS + 1);
      } catch (_) {
        if (!disposed && running && visible() && generation === epoch) markUnavailable("资源读取失败，暂不可用");
      } finally {
        timers.clearTimeout(entry.timeout);
        if (flight === entry) flight = null;
        // 隐藏期间作废的请求尚未释放时，恢复只等它结束，不并发发第二条。
        if (!disposed && running && visible() && generation !== epoch) void refresh();
      }
    })();
    return entry.promise;
  };
  const syncVisibility = () => {
    const next = visible();
    if (next === running) return;
    running = next; epoch += 1;
    if (interval !== null) timers.clearInterval(interval);
    interval = null; clearExpiry();
    if (!running) {
      flight?.controller.abort();
      markUnavailable("资源监控已暂停");
      if (dialog.open) dialog.close();
      return;
    }
    markUnavailable("正在读取资源…");
    void refresh();
    interval = timers.setInterval(() => { if (!fresh() && snapshot) markUnavailable("资源数据已过期，暂不可用"); void refresh(); }, POLL_MS);
  };
  const openDetails = (button) => {
    opener = button?.dataset.resourceOpener ? doc.getElementById(button.dataset.resourceOpener) : button;
    button?.closest("details")?.removeAttribute("open");
    const panel = button?.closest("[popover]");
    if (panel) {
      if (typeof panel.hidePopover === "function") panel.hidePopover();
      panel.hidden = true;
    }
    render();
    if (!dialog.open) dialog.showModal();
    syncVisibility();
    void refresh();
  };
  const onClick = (event) => {
    const button = event.target.closest("[data-resource-details], #workbenchResourceMonitor");
    if (button && root.contains(button)) openDetails(button);
  };
  const onClose = () => {
    const target = opener;
    opener = null;
    if (!target?.isConnected || target.disabled || target.closest("[hidden], [inert]")
        || !target.getClientRects().length || doc.querySelector("dialog[open]")) return;
    target.focus({ preventScroll: true });
  };
  const close = () => dialog.close();
  const stopEscape = (event) => { if (event.key === "Escape") event.stopPropagation(); };
  root.addEventListener("click", onClick);
  dialog.addEventListener("close", onClose);
  dialog.addEventListener("keydown", stopEscape);
  doc.getElementById("resourceMonitorClose")?.addEventListener("click", close);
  doc.addEventListener("visibilitychange", syncVisibility);
  const observer = typeof Observer === "function" ? new Observer(syncVisibility) : null;
  observer?.observe(root, { attributes: true, attributeFilter: ["hidden"] });
  syncVisibility();
  return { refresh, syncVisibility, openDetails, dispose() {
    disposed = true; running = false; epoch += 1;
    if (interval !== null) timers.clearInterval(interval);
    clearExpiry(); flight?.controller.abort(); observer?.disconnect();
    root.removeEventListener("click", onClick);
    dialog.removeEventListener("close", onClose); dialog.removeEventListener("keydown", stopEscape);
    doc.getElementById("resourceMonitorClose")?.removeEventListener("click", close);
    doc.removeEventListener("visibilitychange", syncVisibility);
  } };
}
