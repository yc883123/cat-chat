// ============================================================
// 20-sound.js —— AI 回复完成提示音 + 托盘「任务完成」卡片触发
// ============================================================
// 音效由 Web Audio 现场合成（两音上行「叮咚」），**不打包音频文件**：
// 换音色只改这里，不用碰 git 资产白名单，也不用动打包脚本。
// 托盘完成卡片是桌面壳专属（window.pywebview 存在才可用），浏览器模式自动跳过。

import { state } from "./01-core.js";

let audioCtx = null;
let lastPlayedAt = 0;

function ensureCtx() {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return null;
  if (!audioCtx) audioCtx = new Ctx();
  // 浏览器自动播放策略：音频上下文可能在无交互时被挂起，恢复失败就静默放弃。
  if (audioCtx.state === "suspended") void audioCtx.resume().catch(() => {});
  return audioCtx;
}

function note(ctx, at, freq, dur, vol) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = "sine";
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0, at);
  gain.gain.linearRampToValueAtTime(vol, at + 0.012);
  gain.gain.exponentialRampToValueAtTime(0.0001, at + dur);
  osc.connect(gain).connect(ctx.destination);
  osc.start(at);
  osc.stop(at + dur + 0.05);
}

/**
 * 播一声「叮咚」（A5 → E6 两音上行）。volumePercent：0-100（appearance.done_sound_volume）。
 * force=true 给「试听」按钮用：绕过去重，且忽略开关状态（试听就是要在关着时也能听）。
 */
export function playDoneSound(volumePercent = 60, { force = false } = {}) {
  const now = Date.now();
  // 500ms 去重：重连兜底可能把同一段流事件重放，没有这层会叮两声。
  if (!force && now - lastPlayedAt < 500) return;
  lastPlayedAt = now;
  const ctx = ensureCtx();
  if (!ctx) return;
  const raw = Number(volumePercent);
  const clamped = Number.isFinite(raw) ? Math.min(100, Math.max(0, raw)) : 60;
  const vol = (clamped / 100) * 0.28;
  if (vol <= 0) return;
  const t = ctx.currentTime;
  note(ctx, t, 880, 0.28, vol);
  note(ctx, t + 0.14, 1318.5, 0.5, vol);
}

// ---- 会话完成提醒（提示音 + 托盘卡片共用一个入口、一份 runId 去重）----
// 同一个 run 只提醒一次：runEvents 缓存重放、重复 done 都在这里被挡住。
const notifiedRuns = new Set();

function conversationTitle(conversationId) {
  const found = (state.conversations || []).find((item) => item.id === conversationId);
  return String(found?.title || "").trim() || "Cat Chat";
}

/**
 * 会话完成后的统一提醒入口。由 11-run-stream.js 在收到 type === 'done' 时调用。
 * 开关读 appearance.done_sound / tray_done_toast（服务端 settings 唯一事实来源）。
 */
export function notifyRunDone(conversationId, runId) {
  const key = String(runId || conversationId || "");
  if (!key || notifiedRuns.has(key)) return;
  notifiedRuns.add(key);
  // 防长会话无限增长：超过 200 条时丢掉最早的一半（Set 迭代顺序 = 插入顺序）。
  if (notifiedRuns.size > 200) {
    let drop = Math.ceil(notifiedRuns.size / 2);
    for (const item of notifiedRuns) {
      notifiedRuns.delete(item);
      drop -= 1;
      if (drop <= 0) break;
    }
  }
  const appearance = state.appearance || {};
  if (appearance.done_sound !== false) playDoneSound(appearance.done_sound_volume);

  // 托盘卡片：桌面壳专属。窗口是否真的藏在托盘由**后端**说了算（launcher 知道自己
  // 有没有 hide 过窗口）——SW_HIDE 下 visibilityState 可能仍报 visible，不可信。
  const bridge = window.pywebview?.api;
  if (appearance.tray_done_toast === false || !bridge?.naibaNotifyTaskDone) return;
  if (typeof bridge.naibaWindowHidden === "function") {
    Promise.resolve(bridge.naibaWindowHidden())
      .then((res) => {
        const hidden = res && typeof res === "object" ? Boolean(res.hidden) : Boolean(res);
        if (!hidden) return undefined;
        return bridge.naibaNotifyTaskDone(conversationTitle(conversationId), String(conversationId || ""));
      })
      .catch(() => { /* 桥失败静默：提醒是锦上添花，不能变成报错弹窗 */ });
  } else if (document.visibilityState === "hidden") {
    // 旧桥没有 naibaWindowHidden 时退回页面可见性判断（聊胜于无）。
    Promise.resolve(bridge.naibaNotifyTaskDone(conversationTitle(conversationId), String(conversationId || "")))
      .catch(() => {});
  }
}
