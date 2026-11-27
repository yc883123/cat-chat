// Deterministic frontend execution: real positionComposerPopup from 13-skill-refs.js.
// Guards the 2026-11-26 report: with backdrop-filter on .composer (wallpaper / model skin),
// position:fixed popups resolve against the composer instead of the viewport, so @ and /
// popups were pushed one composer-height below their computed spot and fell off-screen.
// No browser, server, network or clock is involved.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'public', 'js', '13-skill-refs.js'), 'utf8').replace(/\r\n/g, '\n');

function fn(text, name) {
  const match = new RegExp(`^export function ${name}\\(`, 'm').exec(text);
  assert(match, `missing owning function ${name}`);
  const end = text.indexOf('\n}', match.index);
  assert(end > match.index, `unterminated function ${name}`);
  return text.slice(match.index, end + 2).replace(/^export /, '');
}

const positionCode = fn(source, 'positionComposerPopup');

function scenario({ inputRect, viewportHeight = 900, vvHeight = null, popupHeight = 200, inBody = false, hidden = false }) {
  const counts = { append: 0 };
  const body = {
    id: 'body',
    appendChild(node) {
      counts.append += 1;
      node.parentElement = this;
      return node;
    },
  };
  const composer = { id: 'composer', parentElement: body };
  const popup = {
    hidden,
    parentElement: inBody ? body : composer,
    offsetHeight: popupHeight,
    style: {
      removed: [],
      removeProperty(name) { this.removed.push(name); },
    },
  };
  const input = { getBoundingClientRect: () => inputRect };
  const document = { body, querySelector: selector => (selector === '#messageInput' ? input : null) };
  const window = { innerHeight: viewportHeight, visualViewport: { offsetTop: 0, height: vvHeight ?? viewportHeight } };
  const getComputedStyle = () => ({ maxHeight: '300px' });
  const position = new Function('$', 'document', 'window', 'getComputedStyle', `return (${positionCode});`)(
    selector => document.querySelector(selector),
    document,
    window,
    getComputedStyle,
  );
  return { position, popup, counts, body, window };
}

let cases = 0;

// 1. 弹层还在 .composer 里（壁纸/皮肤下带 backdrop-filter）：必须先挂到 body，再按视口坐标放。
{
  const { position, popup, counts, body } = scenario({ inputRect: { top: 800, bottom: 840, left: 100, width: 500 } });
  position(popup);
  assert.equal(popup.parentElement, body, 'popup must be re-parented to document.body');
  assert.equal(counts.append, 1);
  // 输入框上方空间更大 → 贴输入框上方；坐标是视口坐标，不再被 composer 偏移。
  assert.equal(popup.style.top, '594px');
  assert.equal(popup.style.left, '100px');
  assert.equal(popup.style.width, '500px');
  assert.deepEqual(popup.style.removed, ['max-height']);
  cases += 1;
}

// 2. 已经挂在 body（第二次开弹层）：不重复搬动，坐标照常。
{
  const { position, popup, counts, body } = scenario({ inputRect: { top: 800, bottom: 840, left: 100, width: 500 }, inBody: true });
  position(popup);
  assert.equal(popup.parentElement, body);
  assert.equal(counts.append, 0, 'popup already in body must not be appended again');
  assert.equal(popup.style.top, '594px');
  cases += 1;
}

// 3. 输入框贴顶、下方空间更大：弹层放输入框下方，仍以视口坐标为准。
{
  const { position, popup, body } = scenario({ inputRect: { top: 100, bottom: 140, left: 24, width: 640 } });
  position(popup);
  assert.equal(popup.parentElement, body);
  assert.equal(popup.style.top, '146px');
  assert.equal(popup.style.left, '24px');
  cases += 1;
}

// 4. 隐藏的弹层：不动 DOM、不写坐标。
{
  const { position, popup, counts } = scenario({ inputRect: { top: 800, bottom: 840, left: 100, width: 500 }, hidden: true });
  const parent = popup.parentElement;
  position(popup);
  assert.equal(popup.parentElement, parent);
  assert.equal(counts.append, 0);
  assert.equal(popup.style.top, undefined);
  assert.equal(popup.style.left, undefined);
  cases += 1;
}

// 5. 手机软键盘态（可视视口只剩一条）：maxHeight 压进可用空间，仍挂 body、不溢出可视区。
{
  const { position, popup, body } = scenario({ inputRect: { top: 260, bottom: 300, left: 16, width: 320 }, viewportHeight: 700, vvHeight: 300 });
  position(popup);
  assert.equal(popup.parentElement, body);
  assert.equal(popup.style.maxHeight, '254px');
  assert.equal(popup.style.top, '54px');
  cases += 1;
}

// ---- 快捷消息面板：同一类问题（在 .composer-wrap 层叠上下文里被侧栏/输入框盖住）----
const quickCode = fn(fs.readFileSync(path.join(root, 'public', 'js', '12-chat-input.js'), 'utf8').replace(/\r\n/g, '\n'), 'positionQuickMessagePanel');

function quickScenario({ buttonRect, panelWidth = 320, panelHeight = 194, viewport = { width: 1056, height: 711 }, columnLeft = 300, inBody = false, hidden = false }) {
  const counts = { append: 0 };
  const body = { id: 'body', appendChild(node) { counts.append += 1; node.parentElement = this; return node; } };
  const wrapper = { id: 'composer-meta-more', parentElement: body };
  const panel = {
    hidden,
    parentElement: inBody ? body : wrapper,
    offsetWidth: panelWidth,
    offsetHeight: panelHeight,
    style: {},
  };
  const button = { getBoundingClientRect: () => buttonRect };
  const document = {
    body,
    querySelector(selector) {
      if (selector === '#quickMessagePanel') return panel;
      if (selector === '#quickMessageButton') return button;
      if (selector === '.messages') return columnLeft == null ? null : { getBoundingClientRect: () => ({ left: columnLeft }) };
      return null;
    },
  };
  const window = { innerWidth: viewport.width, innerHeight: viewport.height };
  const position = new Function('$', 'document', 'window', `return (${quickCode});`)(
    selector => document.querySelector(selector),
    document,
    window,
  );
  return { position, panel, counts, body };
}

// 6. 窄窗口下按钮靠左：先挂 body，且左边不越过对话列左缘（原来会横跨侧栏、被侧栏盖住）。
{
  const { position, panel, counts, body } = quickScenario({ buttonRect: { top: 671, bottom: 699, left: 308, right: 396 } });
  position(panel);
  assert.equal(panel.parentElement, body, 'quick message panel must be re-parented to document.body');
  assert.equal(counts.append, 1);
  assert.equal(panel.style.left, '300px');
  assert.equal(panel.style.top, '471px');
  cases += 1;
}

// 7. 已经挂在 body：不重复搬动。
{
  const { position, panel, counts, body } = quickScenario({ buttonRect: { top: 671, bottom: 699, left: 308, right: 396 }, inBody: true });
  position(panel);
  assert.equal(panel.parentElement, body);
  assert.equal(counts.append, 0);
  cases += 1;
}

// 8. 手机窄屏（没有对话列可用）：退回视口夹取，不出现负数坐标。
{
  const { position, panel, body } = quickScenario({
    buttonRect: { top: 640, bottom: 668, left: 16, right: 104 },
    viewport: { width: 375, height: 760 },
    columnLeft: null,
  });
  position(panel);
  assert.equal(panel.parentElement, body);
  assert.equal(panel.style.left, '8px');
  cases += 1;
}

// 9. 隐藏状态：不动 DOM、不写坐标。
{
  const { position, panel, counts } = quickScenario({ buttonRect: { top: 671, bottom: 699, left: 308, right: 396 }, hidden: true });
  const parent = panel.parentElement;
  position(panel);
  assert.equal(panel.parentElement, parent);
  assert.equal(counts.append, 0);
  assert.equal(panel.style.top, undefined);
  cases += 1;
}

console.log(`composer popup layer: ${cases} cases passed`);
