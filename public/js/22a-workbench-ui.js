// 工作台共用展示工具：不持有业务状态、不发请求，也不改变执行权限。

export function workbenchWidth(root) {
  return Number(root?.getBoundingClientRect?.().width || root?.clientWidth || 0);
}

export function watchWorkbenchSize(root, callback, { wide = 1000, narrow = 760 } = {}) {
  if (!root) return () => {};
  let previous = "";
  const update = () => {
    const width = workbenchWidth(root);
    // 隐藏页面为零宽，不覆盖最后一个有效档位；重新显示后 ResizeObserver 会校准。
    if (!(width > 0)) return;
    const size = width <= narrow ? "narrow" : width < wide ? "medium" : "wide";
    root.dataset.wbSize = size;
    const key = `${size}:${Math.round(width)}`;
    if (key === previous) return;
    previous = key;
    callback?.({ width, size });
  };
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(update) : null;
  observer?.observe(root);
  window.addEventListener("resize", update);
  update();
  return () => { observer?.disconnect(); window.removeEventListener("resize", update); };
}

export function statusTone(value) {
  const status = String(value || "");
  if (["running", "active", "queued", "generation_running", "reverse_running", "stopping"].includes(status)) return "run";
  if (["waiting_user", "needs_recovery", "human_check", "needs_review", "review", "blocked", "partial"].includes(status)) return "wait";
  if (["done", "completed", "succeeded", "approved", "provided"].includes(status)) return "ok";
  if (["failed", "error", "invalid", "collection_failed", "collect_failed", "download_failed"].includes(status)) return "bad";
  return "idle";
}

export function statusLabel(value, fallback = "状态未知") {
  return ({ draft: "草稿", pending: "待处理", ready: "待开始", queued: "排队中", running: "进行中",
    active: "进行中", waiting_user: "等待确认", needs_recovery: "需要处理", blocked: "等待处理",
    human_check: "需要人工核对", needs_review: "需要人工核对", review: "等待确认", partial: "部分未完成",
    paused: "已暂停", stopping: "停止中", cancelled: "已取消", interrupted: "已中断",
    done: "已完成", completed: "已完成", succeeded: "已完成", failed: "失败", archived: "已归档",
  })[String(value || "")] || fallback;
}

export function patchWorkbenchHtml(element, html) {
  if (!element || element.innerHTML === html) return false;
  element.innerHTML = html;
  return true;
}

export function positionWorkbenchMenu(trigger, panel) {
  if (!trigger || !panel) return;
  const rect = trigger.getBoundingClientRect();
  const viewport = window.visualViewport;
  const left = viewport?.offsetLeft || 0;
  const top = viewport?.offsetTop || 0;
  const width = viewport?.width || document.documentElement.clientWidth;
  const height = viewport?.height || document.documentElement.clientHeight;
  panel.style.maxWidth = `${Math.max(0, width - 24)}px`;
  panel.style.maxHeight = `${Math.max(0, height - 24)}px`;
  const menu = panel.getBoundingClientRect();
  panel.style.left = `${Math.max(left + 12, Math.min(rect.left, left + width - menu.width - 12))}px`;
  panel.style.top = `${Math.max(top + 12, Math.min(rect.bottom + 6, top + height - menu.height - 12))}px`;
}

const menuBindings = new WeakMap();
const detailsRoots = new WeakSet();
const dialogReturnBindings = new WeakSet();

export function bindWorkbenchDialogReturn(dialog, trigger) {
  if (!dialog || !trigger || dialogReturnBindings.has(dialog)) return;
  dialogReturnBindings.add(dialog);
  dialog.addEventListener("close", () => {
    // 原生 dialog 记住的 opener 可能是已经收起的菜单项；仅补救丢失的焦点，
    // 不抢走用户另选的控件，也不把焦点移出接力打开的另一个弹窗。
    const active = document.activeElement;
    if (active && active !== document.body && active !== document.documentElement
        && !dialog.contains(active) && active.isConnected) return;
    if (document.querySelector("dialog[open]")) return;
    if (!trigger.isConnected || trigger.disabled || trigger.closest("[hidden], [inert]")
        || !trigger.getClientRects().length || trigger.checkVisibility?.({ checkVisibilityCSS: true }) === false) return;
    trigger.focus({ preventScroll: true });
  });
}

export function bindWorkbenchDetails(root) {
  if (!root || detailsRoots.has(root)) return;
  detailsRoots.add(root);
  const menus = () => [...root.querySelectorAll("details.wb-media-menu[open], details[data-wb-menu][open]")];
  root.addEventListener("click", (event) => {
    const action = event.target.closest("button,a[href]");
    const menu = action?.closest("details.wb-media-menu, details[data-wb-menu]");
    if (menu && root.contains(menu) && !action.closest("[data-wb-keep-open]")) menu.open = false;
  });
  document.addEventListener("pointerdown", (event) => {
    for (const menu of menus()) if (!menu.contains(event.target)) menu.open = false;
  });
  root.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    const opened = menus();
    for (const menu of opened) menu.open = false;
    if (opened.length) { event.preventDefault(); event.stopPropagation(); opened[0].querySelector("summary")?.focus(); }
  });
}

export function bindWorkbenchMenu(trigger, panel, { onClose, anchor = trigger } = {}) {
  if (!trigger || !panel) return;
  if (menuBindings.has(trigger)) return menuBindings.get(trigger);
  let open = false;
  let openerHadFocus = false;
  panel.setAttribute("popover", "auto");
  trigger.setAttribute("aria-expanded", "false");
  if (panel.id) trigger.setAttribute("aria-controls", panel.id);
  const close = ({ restoreFocus = false } = {}) => {
    if (!open) return;
    open = false;
    if (typeof panel.hidePopover === "function") panel.hidePopover();
    panel.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus && anchor.isConnected) anchor.focus();
    onClose?.();
  };
  const show = () => {
    openerHadFocus = document.activeElement === trigger;
    open = true;
    panel.hidden = false;
    if (typeof panel.showPopover === "function") panel.showPopover();
    // 导出等二级菜单打开时父 popover 会自动关闭，用仍可见的页头按钮锚定和还焦。
    positionWorkbenchMenu(anchor, panel);
    trigger.setAttribute("aria-expanded", "true");
    panel.querySelector("button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled)")?.focus();
  };
  const toggle = () => open ? close({ restoreFocus: true }) : show();
  trigger.addEventListener("click", toggle);
  panel.addEventListener("toggle", (event) => {
    if (event.newState === "closed") close({ restoreFocus: openerHadFocus && (document.activeElement === document.body || panel.contains(document.activeElement)) });
  });
  panel.addEventListener("click", (event) => {
    if (event.target.closest("button,a[href]") && !event.target.closest("[data-wb-keep-open]")) close();
  });
  panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close({ restoreFocus: true }); }
  });
  // 不支持 Popover 的 WebView 仍可关闭；支持时由浏览器负责 light-dismiss。
  document.addEventListener("pointerdown", (event) => {
    if (open && !panel.contains(event.target) && !trigger.contains(event.target)) close();
  });
  const reposition = () => { if (open) positionWorkbenchMenu(anchor, panel); };
  window.addEventListener("resize", reposition);
  window.visualViewport?.addEventListener("resize", reposition);
  window.visualViewport?.addEventListener("scroll", reposition);
  const binding = { close, show };
  menuBindings.set(trigger, binding);
  return binding;
}

export function syncWorkbenchHeader(page) {
  const home = document.querySelector("#studioView > .studio-topbar");
  const target = page === "library" ? home : document.querySelector(`#studioPage${({ novel: "Novel", writer: "Writer", labeler: "Labeler", comfyui: "Comfyui" })[page]} [data-wb-page-header]`);
  if (!home || !target) return;
  const expand = document.getElementById("studioExpandSidebar");
  const mobile = document.getElementById("studioOpenSidebar");
  // 移动原节点，既保留 ID 与事件，也保证切页前后只有一个导航入口。
  if (mobile && mobile.parentElement !== target) target.prepend(mobile);
  if (expand && expand.parentElement !== target) target.prepend(expand);
  const meter = document.getElementById("comfyuiStepMeter");
  if (meter && meter.parentElement !== target) target.append(meter);
  home.hidden = target !== home;
}
