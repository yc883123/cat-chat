// Execute production modules with controlled HTTP completion order.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = name => fs.readFileSync(path.join(__dirname, '../public/js', name), 'utf8')
  .replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const noop = () => {};
function harness() {
  const requests = [], messages = [], views = [], events = [], runs = [];
  const elements = { '#modelSelect': { value: 'online:a' }, '#composerModelSelect': { value: 'model-a' }, '#messageInput': { value: '', focus: noop } };
  const state = { conversationId: 'start', conversations: ['start', 'a', 'b'].map(id => ({ id })), expandedGroups: new Set(), bootstrap: { agents: [], settings: {}, default_model_key: 'online:a' }, providerModelCatalogs: {}, pendingFiles: [] };
  const context = vm.createContext({ console, Set, Map, AbortController, setTimeout, clearTimeout,
    state, $: id => elements[id] || null, api: (url, options = {}) => { const d = deferred(); requests.push({ url, options, ...d }); return d.promise; },
    toast: msg => messages.push(msg), document: { dispatchEvent: e => events.push(e.detail.id), visibilityState: 'visible' },
    CustomEvent: function (name, init) { Object.assign(this, init); },
    renderMessages: rows => views.push(rows), applyConversationModel: c => { elements['#modelSelect'].value = c.model_key; }, applyConversationAgent: noop,
    detachRunSubscription: () => { state.abortController?.abort(); state.abortController = null; state.runConversationId = ''; }, hideChoiceButtons: noop, closeFilePanel: noop, hideFilePopup: noop, clearFileRefCache: noop,
    closeQuickMessagePanel: noop, closePermissionModeMenu: noop, updateDeepReasoningButton: noop, renderPermissionModeSwitch: noop,
    closeSidebar: noop, resumeConversationRun: async id => runs.push(id), composerModelChoice: () => elements['#composerModelSelect'].value,
    resolveNewConversationModel: async () => ({ model_key: elements['#modelSelect'].value, model_name: elements['#composerModelSelect'].value }),
    prefillPresetSkillsInComposer: noop,
    pendingConversationModelSave: () => null,
    conversationModelGeneration: () => 0,
    mergeConversationModelSelection: conversation => conversation,
  });
  vm.runInContext(source('08-conversations.js') + '\nrenderSidebar = renderComposerWorkspace = () => {};', context);
  const pending = (id, suffix = '') => requests.find(r => r.url === `/api/conversations/${id}${suffix}` && !r.used);
  const answer = (id, suffix, value) => { const r = pending(id, suffix); assert.ok(r, `pending ${id}${suffix}`); r.used = true; r.resolve(value); };
  const detail = id => ({ id, model_key: `online:${id}`, model_name: `model-${id}`, messages: [{ content: id }] });
  const complete = async id => { answer(id, '', detail(id)); await tick(); answer(id, '/first_turn', { system: id }); await tick(); };
  return { context, state, requests, messages, views, events, runs, elements, answer, detail, complete };
}
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
test('slow A cannot overwrite B', async () => {
  const h = harness(); const a = h.context.openConversation('a'), b = h.context.openConversation('b');
  await h.complete('b'); await b; await h.complete('a'); await a;
  assert.equal(h.state.conversationId, 'b'); assert.equal(h.views.at(-1)[0].content, 'b');
  assert.equal(h.elements['#modelSelect'].value, 'online:b'); assert.deepEqual(h.events, ['b']);
});
test('first_turn and detail commit together', async () => {
  const h = harness(); const a = h.context.openConversation('a');
  h.answer('a', '', h.detail('a')); await tick();
  assert.equal(h.state.conversationId, 'start');
  const b = h.context.openConversation('b'); await h.complete('b'); await b;
  h.answer('a', '/first_turn', { system: 'a' }); await a;
  assert.equal(h.state.firstTurnInfo.system, 'b'); assert.equal(h.views.at(-1)[0].content, 'b');
});
test('obsolete failure is silent', async () => {
  const h = harness(); const a = h.context.openConversation('a').then(value => ({ value }), error => ({ error }));
  const b = h.context.openConversation('b'); await h.complete('b'); await b;
  h.requests.find(r => r.url === '/api/conversations/a').reject(new Error('old failure'));
  const result = await a; assert.equal(result.error, undefined); assert.equal(result.value, false);
});
test('new conversation invalidates pending history', async () => {
  const h = harness(); const a = h.context.openConversation('a'); const create = h.context.createConversation();
  await tick(); h.requests.find(r => r.url === '/api/conversations').resolve(h.detail('new')); await create;
  await h.complete('a'); await a; assert.equal(h.state.conversationId, 'new');
});
test('an automatic reload cannot supersede a later navigation', async () => {
  const h = harness(); const generation = h.context.conversationOpenGeneration();
  const b = h.context.openConversation('b');
  assert.equal(await h.context.openConversation('a', { expectedGeneration: generation }), false);
  await h.complete('b'); await b; assert.equal(h.state.conversationId, 'b');
});
test('same conversation opens have separate generations', async () => {
  const h = harness(); const old = h.context.openConversation('a'); const latest = h.context.openConversation('a');
  const details = h.requests.filter(r => r.url === '/api/conversations/a');
  const firsts = h.requests.filter(r => r.url.endsWith('/a/first_turn'));
  details[1].resolve({ ...h.detail('a'), messages: [{ content: 'latest' }] }); firsts[1].resolve({ system: 'latest' }); await latest;
  details[0].resolve(h.detail('a')); firsts[0].resolve({ system: 'old' }); assert.equal(await old, false);
  assert.equal(h.views.at(-1)[0].content, 'latest');
});
test('a run started during navigation is detached before committing another conversation', async () => {
  const h = harness(); const opening = h.context.openConversation('b');
  const controller = new AbortController(); h.state.abortController = controller; h.state.runConversationId = 'start';
  await h.complete('b'); await opening;
  assert.equal(controller.signal.aborted, true); assert.equal(h.state.runConversationId, '');
  assert.equal(h.state.conversationId, 'b'); assert.deepEqual(h.runs, ['b']);
});
test('a run started during creation is detached before committing the new conversation', async () => {
  const h = harness(); const creating = h.context.createConversation(); await tick();
  const controller = new AbortController(); h.state.abortController = controller; h.state.runConversationId = 'start';
  h.requests.find(r => r.url === '/api/conversations').resolve(h.detail('new')); await creating;
  assert.equal(controller.signal.aborted, true); assert.equal(h.state.runConversationId, ''); assert.equal(h.state.conversationId, 'new');
});
test('reloading the current conversation keeps its active subscription', async () => {
  const h = harness(); const controller = new AbortController();
  h.state.conversationId = 'a'; h.state.runConversationId = 'a'; h.state.abortController = controller;
  const opening = h.context.openConversation('a'); await h.complete('a'); await opening;
  assert.equal(controller.signal.aborted, false); assert.equal(h.state.abortController, controller);
});
(async () => {
  let failed = 0;
  for (const [name, fn] of tests) { try { await fn(); console.log(`PASS ${name}`); } catch (e) { failed++; console.error(`FAIL ${name}: ${e.stack}`); } }
  console.log(`conversation races: ${tests.length - failed}/${tests.length} passed`); if (failed) process.exitCode = 1;
})();
