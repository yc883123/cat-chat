// ============================================================
// 17-choice-groups.js —— 选择组数据（前端唯一真相，与后端 naiba/core/choices.py 同口径）
// ============================================================
// 只做两件事，都不持有状态、不碰 DOM：
// ① normalizeChoiceGroups：把实时事件 / 消息 metadata / 历史读取三条路径的选项数据规范成
//    契约形态（组结构见 naiba/core/contracts.py 的 CHOICE_GROUP_KEYS / CHOICE_MODES）。
//    规则与后端一致：旧版纯字符串数组 = 一个无题目的组；mode 只认 single|multi，缺省 single；
//    题目与选项**一律不截断**（长度上限只作用于后端的自然语言识别准入）。
// ② 显式 ```naiba-choices 块：解析 + 渲染成「可读题目与选项」的静态预览（有效块不显示原始
//    JSON）；无效块返回空，调用方原样保留为普通代码块（不隐藏、不生成控件）。
//    真正的交互控件在输入框上方的选择面板里（12-chat-input.js）。

import { escapeHtml } from "./01-core.js";

export const EXPLICIT_CHOICE_FENCE = "naiba-choices";

/** 取出组数组：接受 `[组…]` 或 `{"choice_groups": [组…]}` 两种包装。 */
function unwrapGroups(value) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object" && Array.isArray(value.choice_groups)) {
    return value.choice_groups;
  }
  return [];
}

export function normalizeChoiceGroups(choices, choiceGroups = []) {
  const groups = unwrapGroups(choiceGroups);
  const legacy = Array.isArray(choices)
    ? choices.map((choice) => String(choice ?? "").trim()).filter(Boolean)
    : [];
  const raw = groups.length ? groups : (legacy.length ? [{ prompt: "", choices: legacy }] : []);
  return raw.map((group) => ({
    prompt: String(group?.prompt ?? "").trim(),
    choices: Array.isArray(group?.choices)
      ? group.choices.map((choice) => String(choice ?? "").trim()).filter(Boolean)
      : [],
    mode: String(group?.mode ?? "").toLowerCase() === "multi" ? "multi" : "single",
  })).filter((group) => group.choices.length);
}

/** 该代码块的 info string 是否就是显式选项块标记（允许 `naiba-choices json` 这类后缀）。 */
export function isChoiceFence(language) {
  return String(language ?? "").trim().toLowerCase().split(/\s+/)[0] === EXPLICIT_CHOICE_FENCE;
}

/** 解析显式块正文；结构无效（不是 JSON / 无选项）返回空列表，交给自然语言识别兜底。 */
export function explicitChoiceGroups(rawCode) {
  const text = String(rawCode ?? "").trim();
  if (!text) return [];
  let payload = null;
  try {
    payload = JSON.parse(text);
  } catch {
    return [];
  }
  return normalizeChoiceGroups(null, payload);
}

/**
 * 有效显式块 → **折叠成一行**的静态预览：默认只显示「选择题 · 题目（N 个选项）」，
 * 点击展开完整选项列表（交互面板才是答题入口，这里定位为回看）。
 * 空 → 由调用方回退为代码块（不隐藏原始内容）。
 * 「已答/未答」由 04-messages.js 在渲染后按「其后是否出现过 user 消息」补 is-answered。
 */
export function choicePreviewMarkup(groups) {
  if (!Array.isArray(groups) || !groups.length) return "";
  const totalChoices = groups.reduce(
    (sum, group) => sum + (Array.isArray(group?.choices) ? group.choices.length : 0),
    0,
  );
  const subject = String(groups[0]?.prompt || "").trim() || "请选择";
  const meta = groups.length > 1
    ? `${groups.length} 题 · ${totalChoices} 个选项`
    : `${totalChoices} 个选项`;
  const body = groups.map((group, index) => {
    const multi = String(group?.mode ?? "") === "multi";
    const options = (Array.isArray(group?.choices) ? group.choices : [])
      .map((choice) => `<li>${escapeHtml(String(choice ?? ""))}</li>`)
      .join("");
    return `<div class="choice-preview-group">`
      + `<div class="choice-preview-head">`
      + `<span class="choice-preview-index">${index + 1}</span>`
      + `<span class="choice-preview-prompt">${escapeHtml(group?.prompt || "请选择")}</span>`
      + `<span class="choice-mode-badge is-${multi ? "multi" : "single"}">${multi ? "多选" : "单选"}</span>`
      + `</div><ul class="choice-preview-options">${options}</ul></div>`;
  }).join("");
  return `<div class="choice-preview is-collapsed">`
    + `<button type="button" class="choice-preview-toggle" data-choice-preview-toggle aria-expanded="false">`
    + `<span class="choice-preview-caret" aria-hidden="true">▸</span>`
    + `<span class="choice-preview-label">选择题</span>`
    + `<span class="choice-preview-subject">${escapeHtml(subject)}</span>`
    + `<span class="choice-preview-meta">${escapeHtml(meta)}</span>`
    + `</button>`
    + `<div class="choice-preview-body">${body}</div>`
    + `</div>`;
}
