// ============================================================
// 13-skill-refs.js —— 拆分自 public/app.js 第 6069-6328 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, escapeHtml, notifyComposerChanged, state } from "./01-core.js";
import { markdown } from "./02-markdown.js";
export function skillList() { return Array.isArray(state.bootstrap?.skills) ? state.bootstrap.skills : []; }

// 按 /ref 反查 skill：优先 ref，其次 name，忽略大小写。
export function skillByRef(refText) {
  const key = String(refText || '').trim().toLowerCase();
  if (!key) return null;
  return skillList().find((s) => String(s.ref || '').toLowerCase() === key)
    || skillList().find((s) => String(s.name || '').toLowerCase() === key) || null;
}

// 识别文本里所有 <(^|\s)/ref> 且命中了已安装 skill 的引用。
export function tokenizeSkillRefs(text) {
  const matches = [];
  const re = /(^|\s)\/([^\s/#]+)/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const name = m[2];
    const skill = skillByRef(name);
    if (!skill) continue;
    const slashAt = m.index + m[1].length;
    matches.push({ start: slashAt, end: slashAt + 1 + name.length, text: '/' + name, skill });
  }
  return matches;
}

// 供镜像层/气泡：把文本转成带 <span class="skill-ref"> 高亮的 HTML。
export function highlightSkillRefsHtml(text) {
  const tokens = tokenizeSkillRefs(text);
  if (!tokens.length) return escapeHtml(text);
  let out = ''; let pos = 0;
  for (const tok of tokens) {
    out += escapeHtml(text.slice(pos, tok.start));
    out += `<span class="skill-ref">${escapeHtml(tok.text)}</span>`;
    pos = tok.end;
  }
  out += escapeHtml(text.slice(pos));
  return out;
}

// 识别文本里所有 <(^|\s)@…> 引用（@ 前必须是行首或空白；支持 @"含 空格 的路径"）。
// 只做"看起来像引用"的识别，真实是否存在由后端 resolve_file_references 判定。
const FILE_REF_TOKEN_RE = /(^|\s)(@(?:"[^"\n]+"|[^\s@]+))/g;
export function tokenizeFileRefs(text) {
  const matches = [];
  const value = String(text || '');
  FILE_REF_TOKEN_RE.lastIndex = 0;
  let m;
  while ((m = FILE_REF_TOKEN_RE.exec(value)) !== null) {
    const tokenText = m[2];
    const start = m.index + m[1].length;
    matches.push({ start, end: start + tokenText.length, text: tokenText, className: 'file-ref' });
  }
  return matches;
}

// skill 引用 + @ 文件引用合并（按位置排序，重叠时保留先出现的）。
export function composerRefTokens(text) {
  const tokens = [...tokenizeSkillRefs(text), ...tokenizeFileRefs(text)];
  tokens.sort((a, b) => a.start - b.start);
  return tokens;
}

// 镜像层/气泡通用高亮：两类引用都上色。
export function highlightComposerRefsHtml(text) {
  const tokens = composerRefTokens(text);
  if (!tokens.length) return escapeHtml(text);
  let out = ''; let pos = 0;
  for (const tok of tokens) {
    if (tok.start < pos) continue;
    out += escapeHtml(text.slice(pos, tok.start));
    out += `<span class="${tok.className || 'skill-ref'}">${escapeHtml(tok.text)}</span>`;
    pos = tok.end;
  }
  out += escapeHtml(text.slice(pos));
  return out;
}

export function renderInputMirror() {
  const mirror = $('#inputMirror');
  const input = $('#messageInput');
  if (!mirror || !input) return;
  const value = input.value;
  // 空内容时用一个零宽字符撑起镜像层；非空时只放原文本（不额外追加零宽字符，避免影响换行）。
  mirror.innerHTML = value ? highlightComposerRefsHtml(value) : '\u200b';
  mirror.scrollTop = input.scrollTop;
}

// 当前光标所在的那个 <(^|\s)/name…> token；无效时返回 null。
export function currentSlashToken(value, cursor) {
  if (!value || cursor == null) return null;
  const isWS = (ch) => ch === undefined || /\s/.test(ch);
  let i = cursor;
  while (i > 0 && !isWS(value[i - 1])) i--;
  if (i >= cursor || value[i] !== '/') return null;
  if (i > 0 && !isWS(value[i - 1])) return null;
  let j = cursor;
  while (j < value.length && !isWS(value[j])) j++;
  if (cursor < i + 1 || cursor > j) return null;
  return { tokenStart: i, tokenEnd: j, typed: value.slice(i + 1, cursor) };
}

export const popupState = { open: false, selectedIndex: 0, items: [], token: null };

export function positionComposerPopup(popup) {
  const input = $('#messageInput');
  if (!popup || !input || popup.hidden) return;
  const rect = input.getBoundingClientRect();
  // 可视视口感知（2026-09-30 手机截图报障）：Android Chrome 弹出软键盘时布局视口不动、
  // 可视视口缩小且被推到输入框上方（visualViewport.offsetTop > 0）。弹层是 position:fixed、
  // 只拿布局坐标，只看 window.innerHeight 会把上半截顶出可视屏幕 ⇒ 以「当前真正可见的区域」
  // （vv.offsetTop ~ vv.offsetTop + vv.height）为参照；桌面无偏移时两套坐标重合，行为不变。
  const vv = window.visualViewport;
  const vvTop = vv ? vv.offsetTop : 0;
  const vvBottom = vvTop + (vv ? vv.height : window.innerHeight);
  // 先摘上一轮的内联 maxHeight 再读 CSS 上限：内联值若留着会把 getComputedStyle 的读数
  // 钉死在上一轮的约束上，键盘收起时弹层就再也长不回去了。
  popup.style.removeProperty('max-height');
  // 量输入框上/下两侧的可见空间、选大的一侧放弹层；用内联 maxHeight 把弹层压进可用空间
  // （必须先设 maxHeight 再读 offsetHeight，量出来的高才反映约束）。上限仍受 CSS max-height
  // 约束（.file-popup 300 / .skill-popup 260），桌面空间富余时内联值=CSS 值，观感不变。
  const gap = 6;
  const above = rect.top - vvTop;
  const below = vvBottom - rect.bottom;
  const usable = Math.max(above, below) - gap;
  if (usable > 0) {
    const cssCap = Number.parseFloat(getComputedStyle(popup).maxHeight);
    popup.style.maxHeight = `${Math.min(usable, Number.isFinite(cssCap) ? cssCap : Infinity)}px`;
  }
  // else：可视区被挤得贴住输入框，压不下去也放不下——退回 CSS 上限，靠下方夹取兜底。
  const ph = popup.offsetHeight;
  let top = (above >= below) ? rect.top - ph - gap : rect.bottom + gap;
  // 兜底：夹取范围随可视视口走，弹层不许溢出到可视屏幕之外。
  top = Math.max(vvTop + 8, Math.min(top, vvBottom - ph - 8));
  popup.style.left = `${Math.max(8, rect.left)}px`;
  popup.style.width = `${rect.width}px`;
  popup.style.top = `${top}px`;
}

export function positionSkillPopup() {
  positionComposerPopup($('#skillPopup'));
}

export function showSkillPopup(items, selectedIndex, token) {
  const popup = $('#skillPopup');
  if (!popup) return;
  popupState.open = true; popupState.items = items; popupState.token = token; popupState.selectedIndex = selectedIndex;
  if (!items.length) {
    popup.innerHTML = '<div class="skill-popup-empty">没有匹配的 Skill</div>';
    popup.hidden = false;
    positionSkillPopup();
    return;
  }
  popup.innerHTML = items.map((s, i) => `
    <button type="button" role="option" class="skill-popup-item${i === selectedIndex ? ' selected' : ''}" data-skill-index="${i}">
      <div class="skill-popup-main"><b>${escapeHtml(s.name)}</b><em class="skill-size">${s.char_count ? ('~' + s.char_count) : ''}</em></div>
      <div class="skill-popup-sub"><span class="skill-popup-ref">/${escapeHtml(s.ref || s.name)}</span><small>${escapeHtml(s.description || '')}</small></div>
    </button>`).join('');
  popup.querySelector('.selected')?.scrollIntoView({ block: 'nearest' });
  popup.hidden = false;
  positionSkillPopup();
}

export function hideSkillPopup() {
  popupState.open = false; popupState.items = []; popupState.token = null; popupState.selectedIndex = 0;
  const popup = $('#skillPopup');
  // 连同 positionComposerPopup 写入的内联 maxHeight 一起清掉，别把上一轮键盘态的
  // 高度约束带进下一轮打开（visualViewport 感知修复，2026-09-30）。
  if (popup) { popup.hidden = true; popup.style.removeProperty('max-height'); }
}

export function setSkillPopupSelection(index) {
  popupState.selectedIndex = index;
  const popup = $('#skillPopup');
  popup?.querySelectorAll('[data-skill-index]').forEach((el) => {
    el.classList.toggle('selected', Number(el.dataset.skillIndex) === index);
  });
  popup?.querySelector('.selected')?.scrollIntoView({ block: 'nearest' });
}

export function updateSkillPopup() {
  const input = $('#messageInput');
  if (!input) return hideSkillPopup();
  const token = currentSlashToken(input.value, input.selectionStart);
  if (!token) return hideSkillPopup();
  const query = token.typed.toLowerCase();
  const items = skillList().filter((s) =>
    !query || `${s.ref || ''} ${s.name || ''} ${s.description || ''}`.toLowerCase().includes(query));
  items.sort((a, b) => {
    const ap = String(a.ref || '').toLowerCase().startsWith(query) ? 0 : 1;
    const bp = String(b.ref || '').toLowerCase().startsWith(query) ? 0 : 1;
    return ap - bp || String(a.ref || '').localeCompare(String(b.ref || ''));
  });
  showSkillPopup(items, 0, token);
}

export function moveSkillPopupSelection(delta) {
  if (!popupState.open || !popupState.items.length) return;
  const n = popupState.items.length;
  setSkillPopupSelection((popupState.selectedIndex + delta + n) % n);
}

export function commitSkillSelection(skill) {
  const input = $('#messageInput');
  const value = input.value;
  const cursor = input.selectionStart;
  const token = currentSlashToken(value, cursor);
  if (!token) { hideSkillPopup(); return; }
  const replacement = '/' + (skill.ref || skill.name) + ' ';
  const newValue = value.slice(0, token.tokenStart) + replacement + value.slice(token.tokenEnd);
  const newCursor = token.tokenStart + replacement.length;
  input.value = newValue;
  input.setSelectionRange(newCursor, newCursor);
  resizeTextarea();
  renderInputMirror();
  hideSkillPopup();
  notifyComposerChanged(input);
  input.focus();
}

// 在光标处插入文本（必要时补前导空格、末尾补一个空格），供 Skill 引用 / 快捷消息复用。
export function insertTextAtCursor(text, { trailingSpace = true } = {}) {
  const input = $('#messageInput');
  if (!input) return;
  const value = String(text || '');
  const cs = input.selectionStart ?? input.value.length;
  const ce = input.selectionEnd ?? input.value.length;
  const before = input.value.slice(0, cs);
  const after = input.value.slice(ce);
  const needsLeading = cs > 0 && !/\s/.test(input.value[cs - 1]);
  const insertion = (needsLeading ? ' ' : '') + value + (trailingSpace ? ' ' : '');
  input.value = before + insertion + after;
  const newCursor = before.length + insertion.length;
  input.setSelectionRange(newCursor, newCursor);
  resizeTextarea();
  renderInputMirror();
  // 快捷消息 / 技能引用都走这里：改完必须通知输入管线，否则发送按钮不会刷新为可发送。
  notifyComposerChanged(input);
  return newCursor;
}

// 在光标处插入一个 skill 引用（顶栏点击 / 预填复用）。
export function insertSkillRefAtCursor(skill) {
  insertTextAtCursor('/' + (skill.ref || skill.name));
}

// 新会话：把当前 Agent 预设 skill 以 /ref 引用预填到输入框（用户删掉即不引用，统一途径）。
export function prefillPresetSkillsInComposer(conversation) {
  const input = $('#messageInput');
  if (!input) return;
  const agentId = String(conversation?.agent_id || '');
  const agent = (state.bootstrap?.agents || []).find((a) => a.id === agentId);
  const presetIds = new Set((agent?.skill_ids || []).map(String));
  if (!presetIds.size) return;
  const skills = skillList().filter((s) => presetIds.has(String(s.id)));
  if (!skills.length) return;
  input.value = skills.map((s) => '/' + (s.ref || s.name)).join(' ') + ' ';
  resizeTextarea();
  renderInputMirror();
  input.setSelectionRange(input.value.length, input.value.length);
  notifyComposerChanged(input);
  input.focus();
}

// 切换 Agent 后：把该 Agent 预设 Skill 以 /ref 追加到输入框末尾（已在框内的跳过，避免重复）。
export function appendPresetSkillsToComposer(agentId) {
  const input = $('#messageInput');
  if (!input) return;
  const agent = (state.bootstrap?.agents || []).find((a) => String(a.id) === String(agentId));
  const presetIds = new Set((agent?.skill_ids || []).map(String));
  if (!presetIds.size) return;
  const skills = skillList().filter((s) => presetIds.has(String(s.id)));
  if (!skills.length) return;
  const existing = input.value.trimEnd();
  const have = new Set(tokenizeSkillRefs(existing).map((t) => String(t.skill.id)));
  const refs = skills.filter((s) => !have.has(String(s.id))).map((s) => '/' + (s.ref || s.name));
  if (!refs.length) return;
  input.value = existing ? existing + ' ' + refs.join(' ') : refs.join(' ');
  resizeTextarea();
  renderInputMirror();
  input.setSelectionRange(input.value.length, input.value.length);
  notifyComposerChanged(input);
  input.focus();
}

// 解析并返回本消息引用的 skill（去重）。
export function parseSkillReferences(text) {
  const seen = new Set();
  const refs = [];
  for (const tok of tokenizeSkillRefs(text)) {
    if (seen.has(tok.skill.id)) continue;
    seen.add(tok.skill.id);
    refs.push(tok);
  }
  return refs;
}

// 把 /ref 引用从消息文本里剥离（发给模型用）；若剥空则保留原文（纯引用调用场景）。
export function stripSkillReferences(text) {
  const tokens = tokenizeSkillRefs(text);
  if (!tokens.length) return text;
  let out = ''; let pos = 0;
  for (const tok of tokens) {
    out += text.slice(pos, tok.start);
    pos = tok.end;
  }
  out += text.slice(pos);
  const cleaned = out.replace(/\s+/g, ' ').trim();
  return cleaned || text;
}

// 用户气泡：优先显示 display_content（含 /ref 与 @文件引用），并对引用高亮；保留 markdown。
export function renderUserContent(text) {
  const tokens = composerRefTokens(text);
  if (!tokens.length) return markdown(text);
  let protectedText = ''; let pos = 0; let idx = 0;
  const mapping = [];
  for (const tok of tokens) {
    if (tok.start < pos) continue;
    protectedText += text.slice(pos, tok.start);
    const ph = `@@SKILLREF${idx++}@@`;
    mapping.push({ ph, text: tok.text, className: tok.className || 'skill-ref' });
    protectedText += ph;
    pos = tok.end;
  }
  protectedText += text.slice(pos);
  let html = markdown(protectedText);
  for (const m of mapping) {
    html = html.split(m.ph).join(`<span class="${m.className}">${escapeHtml(m.text)}</span>`);
  }
  return html;
}


/**
 * 输入框自增高（唯一写入点）。`MAX_COMPOSER_H` 与 styles.css 的 `.composer textarea { max-height }` 同值。
 *
 * 为什么量高时必须先摘掉 placeholder（§九.128，2026-09-22 用户手机截图报障）：
 * **Chrome 的 `textarea.scrollHeight` 把折行后的 placeholder 也算成内容高度**。手机窄屏里
 * 「回复进行中…（输入后 Enter 加入插话队列）」这句 23 字占位符 + 插话键把输入区挤到 88px，
 * 空输入框被量成 155px 高 —— 表现就是「用了插话之后输入框变成半屏、一个字都没打也降不下来」。
 * 桌面 composer 宽约 880px，同一句一行放得下，所以这个坑只在手机上出现。
 * 摘→量→放回在同一帧内同步完成，不会闪；量高本来就强制一次同步布局，开销不变。
 */
const MAX_COMPOSER_H = 180;
// 展开态上限：占视口高度比例。与 styles.css 的
// `.composer-wrap.is-expanded textarea { max-height: 72dvh }` 同值（改一处必改另一处）。
const MAX_COMPOSER_H_EXPANDED_RATIO = 0.72;

/* 输入框展开/折叠状态。只改高度上限、不动 DOM 结构：
   · 底部 composer 向上生长（锚定视口底部）；
   · 编辑模式下 composer 被搬进气泡、可能位于窗口顶部时向下生长（文档流推挤，
     `.messages` 是滚动容器 ⇒ 只会滚动，永不截断）。
   状态放在本模块是因为 `resizeTextarea()` 是高度的唯一写入点，两者必须同源。 */
let composerExpanded = false;
let composerResizeBound = false;

export function isComposerExpanded() {
  return composerExpanded;
}

function expandedCap() {
  return Math.max(MAX_COMPOSER_H, Math.round(window.innerHeight * MAX_COMPOSER_H_EXPANDED_RATIO));
}

export function setComposerExpanded(next, { focus = false } = {}) {
  composerExpanded = Boolean(next);
  // 编辑模式下 composer-wrap 被搬进气泡：必须带 :not(.edit-placeholder) 排除底部那个
  // hidden 占位锚点（它排在真节点前面，直接查 .composer-wrap 会抓错）。
  const wrap = document.querySelector('.composer-wrap:not(.edit-placeholder)');
  wrap?.classList.toggle('is-expanded', composerExpanded);
  const button = $('#expandComposer');
  if (button) {
    button.setAttribute('aria-pressed', String(composerExpanded));
    const label = composerExpanded ? '收起输入框' : '展开输入框';
    button.title = label;
    button.setAttribute('aria-label', label);
  }
  if (!composerResizeBound) {
    composerResizeBound = true;
    // 展开态的上限是按视口比例算的：窗口尺寸变化时必须重算，否则拖小窗口后
    // 输入框会超出可视区（textarea 自己有滚动条，但会顶掉消息区）。
    window.addEventListener('resize', () => {
      if (composerExpanded) resizeTextarea();
    });
  }
  resizeTextarea();
  if (composerExpanded && focus) $('#messageInput')?.focus();
}

export function toggleComposerExpanded() {
  setComposerExpanded(!composerExpanded, { focus: true });
}

/* 发送/确认编辑后收回折叠态：内容已清空，留着大输入框没有意义。 */
export function collapseComposerIfExpanded() {
  if (composerExpanded) setComposerExpanded(false);
}

export function resizeTextarea() {
  const input = $('#messageInput');
  if (!input) return;
  const placeholder = input.placeholder;
  input.style.height = 'auto';
  if (placeholder) input.placeholder = '';
  try {
    const cap = composerExpanded ? expandedCap() : MAX_COMPOSER_H;
    input.style.height = `${Math.min(input.scrollHeight, cap)}px`;
  } finally {
    if (placeholder) input.placeholder = placeholder;
  }
}

// ---- 右侧文件面板（消息末尾“修改文件”摘要 → 查看 / 富文本编辑）----
