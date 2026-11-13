// 造梦机历史的可见窗口。数据分页与媒体加载由调用方负责，这里只保留视区附近的 DOM。
// 模块载入不依赖浏览器全局，便于在 Node 中执行相关守门。
export function createHistoryViewport(host, options = {}) {
  if (!host) throw new TypeError("历史列表缺少宿主");
  const document = host.ownerDocument;
  const view = document.defaultView;
  const requestFrame = view?.requestAnimationFrame?.bind(view)
    || ((callback) => setTimeout(callback, 16));
  const cancelFrame = view?.cancelAnimationFrame?.bind(view) || clearTimeout;
  const keyOf = options.key || ((item) => item.id);
  const fingerprintOf = options.fingerprint || ((item) => item);
  const estimate = Math.max(1, Number(options.estimateHeight) || 100);
  const gap = Math.max(0, Number(options.gap ?? 8) || 0);
  const shortLimit = 24;
  const overscan = 2;
  const mounted = new Map();
  const measured = new Map();
  let items = [];
  let keys = [];
  let indices = new Map();
  let offsets = [0];
  let rowHeights = [];
  let totalHeight = 0;
  let columnCount = 1;
  let lastWidth = 0;
  let layoutDirty = true;
  let disposed = false;
  let frame = null;
  let pendingAnchor = null;
  let rangeStart = 0;
  let rangeEnd = 0;

  const spacer = () => {
    const node = document.createElement("div");
    node.className = "comfyui-history-spacer";
    node.setAttribute("aria-hidden", "true");
    Object.assign(node.style, { gridColumn: "1 / -1", flexShrink: "0", pointerEvents: "none" });
    return node;
  };
  const before = spacer();
  const after = spacer();
  host.replaceChildren(before, after);

  const visible = () => !disposed && host.clientHeight > 0 && host.clientWidth > 0
    && (!options.isVisible || options.isVisible());
  const paddingTop = () => Number.parseFloat(view?.getComputedStyle(host).paddingTop) || 0;
  const rowAt = (position) => {
    let low = 0;
    let high = rowHeights.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (offsets[middle] + rowHeights[middle] <= position) low = middle + 1;
      else high = middle;
    }
    return Math.min(low, Math.max(0, rowHeights.length - 1));
  };
  const rebuildLayout = () => {
    if (!layoutDirty) return;
    offsets = [0];
    rowHeights = [];
    const rows = Math.ceil(items.length / columnCount);
    for (let row = 0; row < rows; row += 1) {
      let height = 1;
      for (let index = row * columnCount; index < Math.min(items.length, (row + 1) * columnCount); index += 1) {
        height = Math.max(height, measured.get(keys[index]) || estimate);
      }
      rowHeights.push(height);
      offsets.push(offsets[row] + height + gap);
    }
    totalHeight = rows ? offsets[rows] - gap : 0;
    layoutDirty = false;
  };
  const captureAnchor = () => {
    if (host.scrollTop <= 8 || !keys.length) return null;
    const viewportTop = host.getBoundingClientRect().top + host.clientTop;
    if (visible()) {
      const ordered = Array.from(mounted).sort(([left], [right]) => indices.get(left) - indices.get(right));
      for (const [key, entry] of ordered) {
        const bounds = entry.node.getBoundingClientRect();
        if (bounds.bottom > viewportTop + 1 && bounds.top < viewportTop + host.clientHeight) {
          return { key, offset: bounds.top - viewportTop, previous: indices.get(key) || 0 };
        }
      }
    }
    rebuildLayout();
    const row = rowAt(Math.max(0, host.scrollTop - paddingTop()));
    const index = row * columnCount;
    return { key: keys[index], offset: paddingTop() + offsets[row] - host.scrollTop, previous: index };
  };
  const restoreAnchor = (anchor) => {
    if (!anchor || !keys.length) return;
    // 锚点被删时落到相邻位置，避免把整份列表跳回开头。
    const index = indices.has(anchor.key) ? indices.get(anchor.key)
      : Math.min(anchor.previous, keys.length - 1);
    const nextTop = paddingTop() + offsets[Math.floor(index / columnCount)] - anchor.offset;
    if (Math.abs(host.scrollTop - nextTop) > 0.5) host.scrollTop = Math.max(0, nextTop);
  };
  const pauseMedia = (node) => {
    const media = Array.from(node.querySelectorAll?.("video, audio") || []);
    if (node.matches?.("video, audio")) media.unshift(node);
    for (const element of media) {
      try { element.pause(); } catch (_) { /* detached media may already have been released */ }
    }
  };
  const removeEntry = (key, entry) => {
    observer?.unobserve(entry.node);
    pauseMedia(entry.node);
    options.onDispose?.(entry.node, entry.item);
    entry.node.remove();
    mounted.delete(key);
  };
  const setSpacer = (node, height) => {
    const size = Math.max(0, height);
    node.style.height = `${size}px`;
    node.style.display = size > 0 ? "block" : "none";
  };
  const measureMounted = () => {
    let changed = false;
    if (!visible()) return changed;
    for (const [key, entry] of mounted) {
      const height = entry.node.getBoundingClientRect().height;
      if (height > 0 && Math.abs((measured.get(key) || estimate) - height) > 0.5) {
        measured.set(key, height);
        changed = true;
      }
    }
    if (changed) layoutDirty = true;
    return changed;
  };
  const renderRange = () => {
    const showing = visible();
    if (items.length <= shortLimit) {
      rangeStart = 0;
      rangeEnd = items.length;
    } else if (showing) {
      const top = Math.max(0, host.scrollTop - paddingTop());
      const startRow = Math.max(0, rowAt(top) - overscan);
      const endRow = Math.min(rowHeights.length, rowAt(top + host.clientHeight) + overscan + 1);
      rangeStart = startRow * columnCount;
      rangeEnd = Math.min(items.length, endRow * columnCount);
    } else {
      // 隐藏列表只保留原来的小窗口，首次进入时最多准备少量卡片。
      rangeStart = Math.min(rangeStart, Math.max(0, items.length - 1));
      rangeEnd = Math.min(items.length, Math.max(rangeStart, rangeEnd || shortLimit));
    }
    const wanted = new Set(keys.slice(rangeStart, rangeEnd));
    for (const [key, entry] of mounted) {
      if (!wanted.has(key)) removeEntry(key, entry);
    }
    setSpacer(before, rangeStart ? offsets[Math.floor(rangeStart / columnCount)] - gap : 0);
    const endRow = Math.ceil(rangeEnd / columnCount);
    setSpacer(after, rangeEnd < items.length ? totalHeight - offsets[endRow] : 0);
    let cursor = before.nextSibling;
    for (let index = rangeStart; index < rangeEnd; index += 1) {
      const key = keys[index];
      const item = items[index];
      const fingerprint = fingerprintOf(item);
      let entry = mounted.get(key);
      if (entry && entry.fingerprint !== fingerprint) {
        if (cursor === entry.node) cursor = entry.node.nextSibling;
        removeEntry(key, entry);
        entry = null;
      }
      const created = !entry;
      if (created) {
        entry = { node: options.render(item), item, fingerprint };
        mounted.set(key, entry);
      } else entry.item = item;
      if (entry.node !== cursor) host.insertBefore(entry.node, cursor || after);
      cursor = entry.node.nextSibling;
      if (created) {
        observer?.observe(entry.node);
        options.onMount?.(entry.node, item);
      }
    }
  };
  const paint = (anchor = null) => {
    if (disposed) return;
    const width = host.clientWidth;
    const nextColumns = width > 0
      ? Math.max(1, Math.floor(Number(options.columns?.(host)) || 1)) : columnCount;
    if (width > 0 && (Math.abs(lastWidth - width) > 1 || nextColumns !== columnCount)) {
      // 换宽度后，旧的折行高度不能继续用于窗口计算。
      measured.clear();
      lastWidth = width;
      columnCount = nextColumns;
      layoutDirty = true;
    }
    rebuildLayout();
    restoreAnchor(anchor);
    renderRange();
    // 头插后先更新占位高度，再恢复一次锚点；旧 scrollHeight 可能曾把目标位置夹住。
    const previousTop = host.scrollTop;
    restoreAnchor(anchor);
    if (Math.abs(host.scrollTop - previousTop) > 0.5) renderRange();
    if (measureMounted()) {
      const measuredAnchor = anchor || captureAnchor();
      rebuildLayout();
      restoreAnchor(measuredAnchor);
      renderRange();
    }
    if (visible() && items.length
        && totalHeight - host.scrollTop - host.clientHeight <= Math.max(host.clientHeight, estimate)) {
      options.onNearEnd?.();
    }
  };
  const schedule = (preserveAnchor = false) => {
    if (disposed) return;
    if (preserveAnchor && !pendingAnchor) pendingAnchor = captureAnchor();
    if (frame !== null) return;
    frame = requestFrame(() => {
      frame = null;
      const anchor = pendingAnchor;
      pendingAnchor = null;
      paint(anchor);
    });
  };
  const onScroll = () => {
    pendingAnchor = null;
    schedule();
  };
  const ResizeObserver = view?.ResizeObserver;
  const observer = ResizeObserver ? new ResizeObserver(() => schedule(true)) : null;
  observer?.observe(host);
  host.addEventListener("scroll", onScroll, { passive: true });
  view?.addEventListener("resize", schedule);

  return {
    setItems(nextItems) {
      if (disposed) return;
      const anchor = captureAnchor();
      const seen = new Set();
      items = [];
      keys = [];
      for (const item of nextItems || []) {
        const key = String(keyOf(item));
        if (seen.has(key)) continue;
        seen.add(key);
        items.push(item);
        keys.push(key);
      }
      indices = new Map(keys.map((key, index) => [key, index]));
      for (const key of measured.keys()) if (!seen.has(key)) measured.delete(key);
      layoutDirty = true;
      paint(anchor);
    },
    refresh() {
      if (!disposed) paint(captureAnchor());
    },
    visibleKeys() {
      if (!visible()) return [];
      const bounds = host.getBoundingClientRect();
      const top = bounds.top + host.clientTop;
      const bottom = top + host.clientHeight;
      return Array.from(mounted).filter(([, entry]) => {
        const rect = entry.node.getBoundingClientRect();
        return rect.bottom > top && rect.top < bottom;
      }).map(([key]) => key);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      if (frame !== null) cancelFrame(frame);
      observer?.disconnect();
      host.removeEventListener("scroll", onScroll);
      view?.removeEventListener("resize", schedule);
      for (const [key, entry] of mounted) removeEntry(key, entry);
      before.remove();
      after.remove();
      measured.clear();
      items = [];
      keys = [];
      indices.clear();
    },
  };
}
