// 执行实际 Markdown 和卡片桥接代码，替身只负责 DOM、运行时及请求出口。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function read(name) {
  return fs.readFileSync(path.join(__dirname, '..', 'public', 'js', name), 'utf8').replace(/\r\n/g, '\n');
}

function load(context, name) {
  const source = read(name).replace(/^import .*\n/gm, '').replace(/^export /gm, '');
  vm.runInContext(source, context, { filename: name });
}

function functionSource(source, name) {
  const start = source.indexOf(`export ${name === 'sendAuxMessage' ? 'async ' : ''}function ${name}(`);
  assert(start >= 0, name);
  return source.slice(start + 7, source.indexOf('\n}', start) + 2);
}

async function run() {
  const fence = String.fromCharCode(96).repeat(3);
  const json = JSON.stringify({ items: [{ type: 'text', content: '内部' + fence + '还在' }] });
  const ctx = vm.createContext({
    escapeHtml: text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
    normalizeLanguage: value => value,
    restoreSafeHtml: value => value,
    isChoiceFence: () => false,
  });
  load(ctx, '02-markdown.js');
  for (const language of ['dsh-ui', 'json dsh-ui']) {
    const code = `${fence}${language}\n${json}\n${fence}\n`;
    assert.match(ctx.markdown(code, true, { genui: true }), /data-genui-streaming="0"/);
    assert.match(ctx.markdown(code, true, { genui: true }), /还在/);
    assert.doesNotMatch(ctx.markdown(code), /data-genui-fence/);
    assert.doesNotMatch(ctx.markdown(`${fence}${language}\n{}${fence}`, true, { genui: true }), /data-genui-fence/);
    assert.match(ctx.markdown(`${fence}${language}\n{`, true, { genui: true, genuiStreaming: true, highlight: false }), /正在生成互动卡片/);
  }
  assert.doesNotMatch(ctx.markdown(`示例 ${fence}dsh-ui\n{}\n${fence}`, true, { genui: true }), /data-genui-fence/);

  const mounts = [];
  const sends = [];
  const state = { conversationId: 'main' };
  let generation = 1;
  const auxChatState = { activeId: 'aux-a', sending: false };
  const bridge = vm.createContext({
    state, auxChatState, console,
    conversationOpenGeneration: () => generation,
    isConversationOpenCurrent: (expected, id) => expected === generation && id === state.conversationId,
    sendGenuiChatAction: async (...args) => sends.push(['main', ...args]),
    sendGenuiAuxAction: async (...args) => sends.push(['aux', ...args]),
    window: { __CatGenui__: { mount: (_mount, opts) => {
      mounts.push(opts);
      return { rendered: true, unmount() {} };
    } } },
  });
  load(bridge, '33-genui.js');
  function slot(conversationId, auxiliary) {
    const value = { isConnected: true, dataset: {}, classList: { add() {}, remove() {} },
      getClientRects: () => [{}], nodeType: 1, matches: () => true,
      querySelector: name => name === '.genui-raw' ? { value: json } : {},
    };
    const row = { dataset: { genuiConversationId: conversationId, genuiMessageId: 'message' }, querySelectorAll: () => [value] };
    value.closest = selector => {
      if (selector.startsWith('.message-row')) return row;
      if ((selector === '#auxChatMessages') === auxiliary) return {};
      return null;
    };
    return value;
  }
  const main = slot('main', false);
  const auxA = slot('aux-a', true);
  await bridge.hydrateGenuiSlot(main);
  await bridge.hydrateGenuiSlot(auxA);
  assert.match(mounts[0].stateKey, /^main:message:/);
  assert.equal(mounts[1].stateKey, undefined, '辅助卡片不能落持久表单值');
  mounts[0].onAction('confirm', { type: 'button' });
  mounts[1].onAction('confirm', { type: 'button' });
  assert.deepEqual(sends.map(([scope, _text, id]) => [scope, id]), [['main', 'main'], ['aux', 'aux-a']]);
  auxChatState.activeId = 'aux-b';
  mounts[1].onAction('late', {});
  const auxB = slot('aux-b', true);
  await bridge.hydrateGenuiSlot(auxB);
  auxB.isConnected = false;
  mounts[2].onAction('removed', {});
  state.conversationId = 'other';
  mounts[0].onAction('left', {});
  state.conversationId = 'main';
  generation += 2;
  mounts[0].onAction('after-return', {});
  assert.equal(sends.length, 2, '切换或移除后迟到操作不能发送');
  const speculativeStatus = {
    nodeType: 3, data: '已发送 · 仍可修改', parentElement: { closest: () => ({}) },
  };
  bridge.alignVendorActionStatus(speculativeStatus);
  assert.equal(speculativeStatus.data, '已触发 · 仍可修改', '引擎不能替宿主确认请求已发送');

  const input = { value: '保留辅助草稿' };
  const tab = { id: 'aux-b', modelName: 'model', messages: [] };
  const auxRequests = [];
  const aux = vm.createContext({
    $: () => input,
    activeTab: () => tab,
    auxProviderKey: () => 'profile',
    auxChatState,
    filePanelState: { open: true },
    dockGroup: () => 'aux',
    streamAuxRun: async (source, _reply, text) => auxRequests.push([source.id, text]),
    renderAuxChat() {}, toast() {},
  });
  const source = read('21-aux-chat.js');
  vm.runInContext(['sendGenuiAuxAction', 'sendAuxMessage'].map(name => functionSource(source, name)).join('\n'), aux);
  await aux.sendGenuiAuxAction('卡片操作', 'aux-a');
  assert.equal(auxRequests.length, 0);
  await aux.sendGenuiAuxAction('卡片操作', 'aux-b');
  assert.deepEqual(auxRequests, [['aux-b', '卡片操作']]);
  assert.equal(input.value, '保留辅助草稿');
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
