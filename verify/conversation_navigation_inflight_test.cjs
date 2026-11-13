// Execute owning functions with deferred HTTP responses; never copy the guards.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, 'public', 'js', name), 'utf8').replace(/\r\n/g, '\n');
const conversations = read('08-conversations.js');
const models = read('07-models-agents.js');
const runs = read('11-run-stream.js');
const tasks = read('06-tasks-plans.js');
const input = read('12-chat-input.js');

function section(source, start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first, `source section missing: ${start}`);
  return source.slice(first, last);
}

function fn(source, name) {
  const match = new RegExp(`^(?:export )?(?:async )?function ${name}\\(`, 'm').exec(source);
  assert(match, `owning function missing: ${name}`);
  const end = source.indexOf('\n}', match.index);
  assert(end >= 0);
  return source.slice(match.index, end + 2);
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness() {
  const rows = new Map(['A', 'B'].map(id => [id, {
    id, model_key: 'p', model_name: id === 'A' ? 'M1' : 'MB', messages: [],
  }]));
  const detailGates = new Map(), runGates = new Map();
  const calls = [], notices = [], sent = [];
  const elements = {
    '#modelSelect': { value: 'p', querySelectorAll: () => [], prepend() {} },
    '#composerModelSelect': { value: 'M1' },
    '#agentSelect': { value: 'agent' },
    '#messageInput': { value: '', focus() {} },
  };
  const h = { rows, detailGates, runGates, calls, notices, sent, elements,
    settingsGate: null, createGate: null, createEntered: deferred(), providerGates: [] };
  const state = {
    conversationId: 'A', conversations: [...rows.values()].map(row => ({ ...row })),
    expandedGroups: new Set(), cancelledRunIds: new Set(), providerModelCatalogs: {},
    runGeneration: 1, runReconnectAt: 0, runReconnectTimers: new Set(),
    bootstrap: { agents: [{ id: 'agent' }], default_agent_id: 'agent', settings: {},
      model_profiles: [{ id: 'p', model_key: 'p' }, { id: 'q', model_key: 'q' }] },
  };
  const context = {
    console: { log() {}, warn() {}, debug() {} }, Map, Set, Promise, AbortController, Date,
    state, $: selector => elements[selector] || null,
    toast: message => notices.push(message),
    api: async (url, options = {}) => {
      calls.push({ url, method: options.method || 'GET', body: options.body });
      if (url === '/api/providers/models') {
        const gate = h.providerGates.shift();
        assert(gate, 'unexpected provider lookup');
        return gate.promise;
      }
      if (url === '/api/conversations' && options.method === 'POST') {
        h.createEntered.resolve();
        return h.createGate.promise;
      }
      const save = /^\/api\/conversations\/([^/]+)\/settings$/.exec(url);
      if (save) {
        if (h.settingsGate) await h.settingsGate.promise;
        const row = { ...rows.get(save[1]), ...options.body };
        rows.set(save[1], row);
        return { ...row };
      }
      if (url.endsWith('/tools')) return { added: [] };
      if (url.includes('/api/runs?conversation_id=')) {
        const id = new URL(url, 'http://fixture.invalid').searchParams.get('conversation_id');
        return runGates.has(id) ? runGates.get(id).promise : { runs: [] };
      }
      if (url.endsWith('/first_turn')) return null;
      const detail = /^\/api\/conversations\/([^/]+)$/.exec(url);
      if (detail) return detailGates.has(detail[1]) ? detailGates.get(detail[1]).promise : { ...rows.get(detail[1]) };
      throw new Error(`Unexpected request: ${url}`);
    },
    currentConversationWorkspaceGroup: () => '', conversationSnapshot: () => '',
    document: { dispatchEvent() {}, querySelectorAll: () => [],
      createElement: () => ({ dataset: {} }) },
    CustomEvent: class {},
    selectedProvider: () => state.bootstrap.model_profiles.find(row => row.model_key === elements['#modelSelect'].value),
    composerModelChoice: () => elements['#composerModelSelect'].value,
    populateComposerModels: async preferred => { elements['#composerModelSelect'].value = preferred; },
    refreshKvmemStatus: async () => null,
    applyModelSkinFor: async () => {}, resolveNewConversationModel: async () => ({ model_key: 'p', model_name: 'M1' }),
    COMPOSER_MODEL_REFRESH: '__refresh_composer_models__',
    fetchRunEvents: () => new Promise(() => {}), createRunRow: () => ({ isConnected: true }),
    loadTasks: async () => {}, sendMessage: text => sent.push(text),
    SKILL_INSTALL_PRESET: 'install preset', SKILL_EDIT_PRESET: 'edit preset',
  };
  context.detachRunSubscription = () => {
    state.abortController = null; state.runConversationId = ''; state.chatRunId = '';
    state.runGeneration += 1;
  };
  context.detachRunConnection = () => { state.abortController = null; state.runGeneration += 1; };
  for (const name of [
    'hideChoiceButtons', 'closeFilePanel', 'hideFilePopup', 'clearFileRefCache', 'closeQuickMessagePanel',
    'closePermissionModeMenu', 'renderComposerWorkspace', 'renderSidebar', 'applyConversationAgent',
    'renderMessages', 'renderPermissionModeSwitch', 'updateDeepReasoningButton', 'closeSidebar',
    'setBusy', 'updateUnloadModelButton', 'renderKvmemControl', 'prefillPresetSkillsInComposer',
  ]) context[name] = () => {};
  const source = [
    section(conversations, 'let openGeneration', 'export async function loadConversations'),
    section(models, 'let modelSelectionGeneration', 'export async function loadModelCatalog'),
    'let composerModelLastChoice = "";',
    ...['openConversation', 'createConversation'].map(name => fn(conversations, name)),
    ...['applyConversationModel', 'persistConversationModelName', 'saveComposerModelSelection', 'loadModelCatalog'].map(name => fn(models, name)),
    ...['resumeRun', 'resumeConversationRun', 'finishRunSubscription'].map(name => fn(runs, name)),
    fn(tasks, 'maybeRecoverRunFromPoll'),
    ...['startSkillInstall', 'startSkillEdit'].map(name => fn(input, name)),
  ].join('\n').replace(/^export /gm, '');
  vm.createContext(context);
  vm.runInContext(source, context, { filename: 'conversation owning functions' });
  h.context = context;
  return h;
}

const cases = [];
function test(name, body) { cases.push({ name, body }); }

test('model save wins over older same-conversation detail response', async () => {
  const h = harness(), gate = deferred();
  const old = { ...h.rows.get('A') };
  h.detailGates.set('A', gate);
  const opening = h.context.openConversation('A');
  await h.context.queueConversationModelSave('A', { model_key: 'p', model_name: 'M2' });
  gate.resolve(old);
  assert.equal(await opening, true);
  assert.equal(h.rows.get('A').model_name, 'M2');
  assert.equal(h.context.state.conversations.find(row => row.id === 'A').model_name, 'M2');
  assert.equal(h.elements['#composerModelSelect'].value, 'M2');
});

test('save failure remains visible after a same-conversation reload', async () => {
  const h = harness(), detail = deferred(), save = deferred();
  h.detailGates.set('A', detail); h.settingsGate = save;
  const opening = h.context.openConversation('A');
  h.elements['#composerModelSelect'].value = 'M2';
  const saving = h.context.saveComposerModelSelection();
  detail.resolve({ ...h.rows.get('A') });
  await opening;
  save.reject(new Error('fixture save failed'));
  await saving;
  assert.equal(h.rows.get('A').model_name, 'M1');
  assert(h.notices.some(message => message.includes('保存模型失败')), 'same-conversation reload swallowed save failure');
});

test('old run recovery cannot attach between navigation start and detail commit', async () => {
  const h = harness(), run = deferred(), detail = deferred();
  h.context.state.checkRunEligible = true;
  h.runGates.set('A', run); h.detailGates.set('B', detail);
  const recovery = h.context.maybeRecoverRunFromPoll();
  const opening = h.context.openConversation('B');
  run.resolve({ runs: [{ id: 'run-A', conversation_id: 'A', kind: 'chat' }] });
  await recovery;
  detail.resolve({ ...h.rows.get('B') });
  await opening;
  assert.equal(h.context.state.conversationId, 'B');
  assert.notEqual(h.context.state.runConversationId, 'A');
  assert.notEqual(h.context.state.chatRunId, 'run-A');
});

test('old failed recovery cannot set reconnect state on the new navigation', async () => {
  const h = harness(), run = deferred(), detail = deferred();
  h.context.state.checkRunEligible = true;
  h.runGates.set('A', run); h.detailGates.set('B', detail);
  const recovery = h.context.maybeRecoverRunFromPoll();
  const opening = h.context.openConversation('B');
  run.reject(new Error('fixture disconnect'));
  await recovery;
  assert.equal(h.context.state.runReconnectAt, 0);
  detail.resolve({ ...h.rows.get('B') });
  await opening;
});

test('late final reload cannot override a navigation whose detail is pending', async () => {
  const h = harness(), detail = deferred();
  h.detailGates.set('B', detail);
  const opening = h.context.openConversation('B');
  await h.context.finishRunSubscription('A', {});
  assert(!h.calls.some(call => call.url === '/api/conversations/A'), 'late final reload reopened A');
  detail.resolve({ ...h.rows.get('B') });
  await opening;
  assert.equal(h.context.state.conversationId, 'B');
});

for (const action of ['startSkillInstall', 'startSkillEdit']) {
  test(`${action} stops when its new conversation was superseded`, async () => {
    const h = harness();
    h.context.state.conversationId = '';
    h.createGate = deferred();
    const starting = h.context[action]();
    await h.createEntered.promise;
    await h.context.openConversation('B');
    h.createGate.resolve({ id: 'C', model_key: 'p', model_name: 'M1' });
    await starting;
    assert.equal(h.context.state.conversationId, 'B');
    assert(!h.calls.some(call => call.url.endsWith('/tools')), 'superseded creation modified another conversation tools');
    assert.equal(h.sent.length, 0, 'superseded creation sent the preset into another conversation');
  });
}

test('composer and settings share an in-flight catalog lookup', async () => {
  const h = harness(), gate = deferred();
  h.providerGates.push(gate);
  const first = h.context.loadModelCatalog('p');
  const second = h.context.loadModelCatalog('p');
  assert.equal(h.calls.filter(call => call.url === '/api/providers/models').length, 1);
  gate.resolve({ models: [{ id: 'M2' }] });
  assert.equal((await first)[0].id, 'M2');
  assert.equal((await second)[0].id, 'M2');
});

test('superseded catalog request waits for the latest force refresh', async () => {
  const h = harness(), old = deferred(), fresh = deferred();
  h.providerGates.push(old, fresh);
  const first = h.context.loadModelCatalog('p');
  const latest = h.context.loadModelCatalog('p', { force: true });
  let firstSettled = false;
  void first.then(() => { firstSettled = true; });
  old.resolve({ models: [{ id: 'M1' }] });
  await Promise.resolve(); await Promise.resolve();
  assert.equal(firstSettled, false, 'old response rendered a catalog before the newer lookup finished');
  fresh.resolve({ models: [{ id: 'M2' }] });
  assert.equal((await first)[0].id, 'M2');
  assert.equal((await latest)[0].id, 'M2');
  assert.equal(h.context.state.providerModelCatalogs.p[0].id, 'M2');
});

(async () => {
  let failures = 0;
  for (const item of cases) {
    try { await item.body(); }
    catch (error) { failures += 1; console.error(`FAIL: ${item.name}\n${error.stack}`); }
  }
  if (failures) throw new Error(`${failures}/${cases.length} conversation inflight cases failed`);
  console.log(`conversation navigation inflight: ${cases.length} cases passed`);
})().catch(error => { console.error(error.message); process.exitCode = 1; });
