const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../public/js/07-models-agents.js'), 'utf8')
  .replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function harness() {
  const calls = [], messages = [];
  const elements = { '#modelSelect': { value: 'online:new' }, '#composerModelSelect': { value: 'new-model' } };
  const state = { conversationId: 'a', conversations: [{ id: 'a', model_key: 'online:a' }, { id: 'b', model_key: 'online:b' }],
    bootstrap: { settings: {}, default_model_key: 'online:a', model_profiles: [{ model_key: 'online:new' }] }, providerModelCatalogs: {} };
  const context = vm.createContext({ console, state, $: id => elements[id] || null, Map, Set,
    api: (url, options) => new Promise((resolve, reject) => calls.push({ url, options, resolve, reject })),
    toast: msg => messages.push(msg), applyModelSkinFor: async () => {},
  });
  vm.runInContext(source + '\nupdateUnloadModelButton = renderKvmemControl = () => {}; refreshKvmemStatus = async () => {}; populateComposerModels = async () => {};', context);
  return { context, state, calls, elements, messages };
}
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
test('API save targets the original conversation and never the global settings', async () => {
  const h = harness(); const p = h.context.saveModelSelection(); await tick();
  h.state.conversationId = 'b'; h.calls[0].resolve({ settings: {}, id: 'a', model_key: 'online:new' }); await tick();
  if (h.calls[1]) h.calls[1].resolve({ id: 'b', model_key: 'online:new' }); await p;
  assert.deepEqual(h.calls.map(c => c.url), ['/api/conversations/a/settings']);
  assert.equal(h.state.conversations[1].model_key, 'online:b'); assert.equal(h.state.bootstrap.default_model_key, 'online:a');
});
test('model save includes API and late response never merges into B', async () => {
  const h = harness(); const p = h.context.saveComposerModelSelection(); await tick(); h.state.conversationId = 'b';
  h.calls[0].resolve({ id: 'a', model_key: 'online:new', model_name: 'new-model' }); await p;
  assert.equal(h.calls[0].options.body.model_key, 'online:new');
  assert.equal(h.state.conversations[1].id, 'b'); assert.equal(h.state.conversations[1].model_key, 'online:b');
});
test('same conversation queues clearing API then complete model selection', async () => {
  const h = harness(); const apiSave = h.context.saveModelSelection(); await tick();
  const modelSave = h.context.saveComposerModelSelection(); await tick(); assert.equal(h.calls.length, 1);
  h.calls[0].resolve({ id: 'a', model_key: 'online:new', model_name: '' }); await tick();
  assert.equal(h.calls[1].options.body.model_name, 'new-model');
  h.calls[1].resolve({ id: 'a', model_key: 'online:new', model_name: 'new-model' }); await Promise.all([apiSave, modelSave]);
  assert.equal(h.state.conversations[0].model_name, 'new-model');
});
test('another conversation does not wait for A save', async () => {
  const h = harness(); const a = h.context.saveComposerModelSelection(); await tick(); h.state.conversationId = 'b';
  const b = h.context.saveComposerModelSelection(); await tick(); assert.equal(h.calls.length, 2);
  h.calls[1].resolve({ id: 'b', model_name: 'b-model' }); await b; h.calls[0].resolve({ id: 'a', model_name: 'a-model' }); await a;
  assert.equal(h.state.conversations[0].model_name, 'a-model'); assert.equal(h.state.conversations[1].model_name, 'b-model');
});
test('new conversation uses complete valid default, otherwise composer, never first catalog item', async () => {
  const h = harness(); h.state.bootstrap.settings = { new_conversation_model_key: 'online:new', new_conversation_model_name: 'default-model' };
  h.state.providerModelCatalogs['online:new'] = [{ id: 'first' }, { id: 'default-model' }];
  assert.equal((await h.context.resolveNewConversationModel()).model_name, 'default-model');
  h.state.providerModelCatalogs['online:new'] = [{ id: 'first' }];
  assert.equal((await h.context.resolveNewConversationModel()).model_name, 'new-model');
  h.state.bootstrap.settings = {};
  assert.equal((await h.context.resolveNewConversationModel()).model_name, 'new-model');
});

class OptionStub {
  constructor(text = '', value = '') { this.textContent = text; this.value = value; this.dataset = {}; }
  remove() { if (this.parent) this.parent.options = this.parent.options.filter(item => item !== this); }
}
class SelectStub {
  constructor(options = [], value = '') {
    this.options = []; this.dataset = {}; this.disabled = false; this._value = '';
    options.forEach(item => this.append(new OptionStub(item, item))); this.value = value;
  }
  get value() { return this._value; }
  set value(value) { this._value = this.options.some(item => item.value === value) ? value : ''; }
  append(option) { option.parent = this; this.options.push(option); }
  prepend(option) { option.parent = this; this.options.unshift(option); }
  replaceChildren(...options) { this.options = []; options.forEach(item => this.append(item)); this._value = options[0]?.value || ''; }
  querySelectorAll() { return this.options.filter(item => item.dataset.missingBinding); }
  addEventListener() {}
}
const settingsSource = fs.readFileSync(path.join(__dirname, '../public/js/09-settings.js'), 'utf8');
const defaultSource = settingsSource.slice(settingsSource.indexOf('let newDefaultGeneration = 0;'),
  settingsSource.indexOf('export function openProviderCard')).replace(/^export /gm, '');
const editorSource = settingsSource.slice(settingsSource.indexOf('let providerModelCheckGeneration = 0;'),
  settingsSource.indexOf('export let providerModelCheckTimer')).replace(/^export /gm, '');
function domHarness() {
  const calls = [], messages = [];
  const profiles = [
    { id: 'a', model_key: 'online:a', name: 'API A' },
    { id: 'b', model_key: 'local:b', name: 'API B', kind: 'local' },
  ];
  const elements = {
    '#modelSelect': new SelectStub(['online:a', 'local:b'], 'online:a'),
    '#composerModelSelect': new SelectStub(['chosen-a'], 'chosen-a'),
    '#newConversationDefaultApi': new SelectStub(), '#newConversationDefaultModel': new SelectStub(),
    '#newConversationDefaultCheck': { addEventListener() {} }, '#newConversationDefaultSave': { addEventListener() {} },
    '#newConversationDefaultClear': { addEventListener() {} }, '#newConversationDefaultStatus': {},
  };
  elements['#newConversationDefault'] = { dataset: {}, querySelectorAll: () => Object.entries(elements)
    .filter(([key]) => /Default(Api|Model|Check|Save|Clear)$/.test(key)).map(([, element]) => element) };
  const state = { conversationId: 'a', conversations: [{ id: 'a', model_key: 'online:a', model_name: 'chosen-a' }],
    bootstrap: { settings: {}, default_model_key: 'online:a', model_profiles: profiles, providers: profiles },
    providerModelCatalogs: { 'online:a': [{ id: 'chosen-a' }], 'local:b': [{ id: 'chosen-b' }] } };
  const context = vm.createContext({ console, state, $: id => elements[id] || null, Map, Set,
    Option: OptionStub, document: { createElement: () => new OptionStub() },
    api: (url, options) => new Promise((resolve, reject) => calls.push({ url, options, resolve, reject })),
    toast: msg => messages.push(msg), applyModelSkinFor: async () => {}, providerProfiles: () => profiles,
  });
  vm.runInContext(source + '\n' + defaultSource + '\nupdateUnloadModelButton = renderKvmemControl = syncComposerModelPicker = () => {}; refreshKvmemStatus = async () => {}; function renderProviders() { renderNewConversationDefault(); }', context);
  return { context, state, calls, elements, messages, profiles };
}
function editorHarness() {
  const h = domHarness(); h.state.providerEditing = true;
  h.elements['#loadProviderModels'] = {}; h.elements['#providerError'] = {};
  h.elements['#providerModel'] = { value: 'chosen-a' };
  Object.assign(h.profiles[0], { base_url: 'http://127.0.0.1:9', request_format: 'openai_chat', kind: 'online', api_key: '', has_api_key: true });
  h.form = { id: 'a', kind: 'online', base_url: 'http://127.0.0.1:9', request_format: 'openai_chat', api_key: '' };
  h.context.providerFormValue = () => ({ ...h.form });
  h.context.setProviderModelOptions = models => { h.renderedModels = models; };
  h.context.applyProviderModelCapabilities = () => {};
  vm.runInContext(editorSource, h.context);
  return h;
}
test('history without an API displays unbound and preserves its saved model without writes', async () => {
  const h = domHarness(); h.context.applyConversationModel({ id: 'legacy', model_name: 'saved-legacy' }); await tick();
  assert.equal(h.elements['#modelSelect'].value, '');
  assert.match(h.elements['#modelSelect'].options[0].textContent, /未设置 API/);
  assert.equal(h.elements['#composerModelSelect'].value, 'saved-legacy');
  assert.equal(h.calls.length, 0); assert.equal(h.state.bootstrap.default_model_key, 'online:a');
});
test('history with a deleted API keeps an identifiable binding and never chooses the common default', async () => {
  const h = domHarness(); h.context.applyConversationModel({ id: 'deleted', model_key: 'online:deleted', model_name: 'old-model' }); await tick();
  assert.equal(h.elements['#modelSelect'].value, 'online:deleted');
  assert.match(h.elements['#modelSelect'].options[0].textContent, /已删除.*online:deleted/);
  assert.equal(h.elements['#composerModelSelect'].value, 'old-model'); assert.equal(h.calls.length, 0);
});
test('legacy provider IDs resolve unique local profiles and refuse ambiguous profiles', async () => {
  const h = domHarness(); h.context.applyConversationModel({ id: 'legacy', provider_id: 'b', model_name: 'chosen-b' }); await tick();
  assert.equal(h.elements['#modelSelect'].value, 'local:b');
  assert.equal(h.elements['#composerModelSelect'].value, 'chosen-b');
  h.profiles.push({ id: 'b', model_key: 'online:b' });
  h.context.applyConversationModel({ id: 'legacy', provider_id: 'b', model_name: 'chosen-b' }); await tick();
  assert.equal(h.elements['#modelSelect'].value, ''); assert.equal(h.calls.length, 0);
});
test('missing saved models remain selected after the catalog arrives and no history settings are written', async () => {
  const h = domHarness(); delete h.state.providerModelCatalogs['online:a'];
  h.context.applyConversationModel({ id: 'a', model_key: 'online:a', model_name: 'removed-model' }); await tick();
  assert.equal(h.calls.length, 1); h.calls[0].resolve({ models: [{ id: 'first' }] }); await tick();
  assert.equal(h.elements['#composerModelSelect'].value, 'removed-model');
  assert.match(h.elements['#composerModelSelect'].options[0].textContent, /已保存.*重新检查/);
  assert.deepEqual(h.calls.map(call => call.url), ['/api/providers/models']);
});
test('newer forced catalog wins when two checks return in reverse order', async () => {
  const h = domHarness(); const old = h.context.loadModelCatalog('online:a', { force: true });
  const latest = h.context.loadModelCatalog('online:a', { force: true });
  h.calls[1].resolve({ models: [{ id: 'latest' }] }); await latest;
  h.calls[0].resolve({ models: [{ id: 'old' }] }); await old;
  assert.equal(h.state.providerModelCatalogs['online:a'][0].id, 'latest');
});
test('catalog refresh cannot overwrite a later model choice in the same conversation', async () => {
  const h = domHarness(); const refresh = h.context.populateComposerModels('chosen-a', { force: true });
  h.elements['#composerModelSelect'].append(new OptionStub('later-choice', 'later-choice'));
  h.elements['#composerModelSelect'].value = 'later-choice';
  h.calls[0].resolve({ models: [{ id: 'chosen-a' }, { id: 'later-choice' }] }); await refresh;
  assert.equal(h.elements['#composerModelSelect'].value, 'later-choice');
});
test('deleted or unavailable new defaults fall back to the composer snapshot', async () => {
  const h = domHarness(); h.state.bootstrap.settings = { new_conversation_model_key: 'online:deleted', new_conversation_model_name: 'deleted' };
  assert.equal((await h.context.resolveNewConversationModel()).model_key, 'online:a'); assert.equal(h.calls.length, 0);
  h.state.bootstrap.settings = { new_conversation_model_key: 'local:b', new_conversation_model_name: 'chosen-b' };
  delete h.state.providerModelCatalogs['local:b'];
  const result = h.context.resolveNewConversationModel();
  h.elements['#modelSelect'].value = 'local:b';
  h.calls[0].reject(new Error('directory offline'));
  const chosen = await result; assert.equal(chosen.model_key, 'online:a'); assert.equal(chosen.model_name, 'chosen-a');
});
test('default save failure restores the saved pair and leaves common default and history unchanged', async () => {
  const h = domHarness(); h.state.bootstrap.settings = { new_conversation_model_key: 'online:a', new_conversation_model_name: 'chosen-a' };
  h.context.renderNewConversationDefault();
  h.elements['#newConversationDefaultApi'].value = 'local:b';
  h.elements['#newConversationDefaultModel'].append(new OptionStub('chosen-b', 'chosen-b'));
  h.elements['#newConversationDefaultModel'].value = 'chosen-b';
  const saving = h.context.saveNewConversationDefault();
  assert.equal(h.elements['#newConversationDefaultApi'].disabled, true);
  h.calls[0].reject(new Error('disk unavailable')); await saving;
  assert.equal(h.elements['#newConversationDefaultApi'].value, 'online:a');
  assert.equal(h.elements['#newConversationDefaultModel'].value, 'chosen-a');
  assert.equal(h.state.bootstrap.default_model_key, 'online:a');
  assert.equal(h.state.conversations[0].model_key, 'online:a');
  assert.deepEqual(h.calls.map(call => call.url), ['/api/settings']);
  assert.match(h.messages.at(-1), /默认保存失败.*disk unavailable/);
});
test('saving the new default changes both settings together without rebinding current history', async () => {
  const h = domHarness(); h.context.renderNewConversationDefault();
  h.elements['#newConversationDefaultApi'].value = 'local:b';
  h.elements['#newConversationDefaultModel'].append(new OptionStub('chosen-b', 'chosen-b'));
  h.elements['#newConversationDefaultModel'].value = 'chosen-b';
  const saving = h.context.saveNewConversationDefault();
  assert.equal(h.calls[0].options.body.new_conversation_model_key, 'local:b');
  assert.equal(h.calls[0].options.body.new_conversation_model_name, 'chosen-b');
  h.calls[0].resolve({ settings: { new_conversation_model_key: 'local:b', new_conversation_model_name: 'chosen-b' }, default_model_key: 'local:b' }); await saving;
  assert.equal(h.state.bootstrap.default_model_key, 'local:b'); assert.equal(h.profiles[1].is_default, true);
  assert.equal(h.state.conversations[0].model_key, 'online:a'); assert.equal(h.elements['#modelSelect'].value, 'online:a');
});
test('an older provider editor check cannot replace a newer default catalog check', async () => {
  const h = editorHarness(); const older = h.context.loadProviderModels();
  const latest = h.context.loadModelCatalog('online:a', { force: true });
  h.calls[1].resolve({ models: [{ id: 'fresh' }] }); await latest;
  h.calls[0].resolve({ models: [{ id: 'stale' }] }); await older;
  assert.equal(h.state.providerModelCatalogs['online:a'][0].id, 'fresh');
  assert.equal(h.renderedModels[0].id, 'fresh');
});
test('an older provider editor failure cannot erase a newer successful catalog', async () => {
  const h = editorHarness(); const older = h.context.loadProviderModels();
  const latest = h.context.loadModelCatalog('online:a', { force: true });
  h.calls[1].resolve({ models: [{ id: 'fresh' }] }); await latest;
  h.calls[0].reject(new Error('old request failed')); await older;
  assert.equal(h.state.providerModelCatalogs['online:a'][0].id, 'fresh');
});
test('a catalog reply for an edited form cannot redraw the different provider being edited', async () => {
  const h = editorHarness(); const older = h.context.loadProviderModels();
  h.form = { ...h.form, id: 'b', kind: 'local', base_url: 'http://127.0.0.1:10' };
  h.elements['#providerError'].textContent = 'second provider';
  h.calls[0].resolve({ models: [{ id: 'first-provider-only' }] }); await older;
  assert.equal(h.renderedModels, undefined); assert.equal(h.elements['#providerError'].textContent, 'second provider');
  assert.equal(h.messages.length, 0);
});
test('saved connection checks share the catalog and preserve the stored key through model_key resolution', async () => {
  const h = editorHarness();
  h.form = { ...h.form, base_url: `${h.form.base_url}/`, request_format: 'OPENAI_CHAT', name: 'unsaved label', model: 'unsaved model' };
  const checked = h.context.loadProviderModels();
  assert.deepEqual(Object.keys(h.calls[0].options.body), ['model_key']);
  assert.equal(h.calls[0].options.body.model_key, 'online:a');
  h.calls[0].resolve({ models: [{ id: 'saved-connection-model' }] }); await checked;
  assert.equal(h.state.providerModelCatalogs['online:a'][0].id, 'saved-connection-model');
  assert.equal(h.renderedModels[0].id, 'saved-connection-model');
});
test('unsaved URL, format, kind or key checks stay local to the provider form', async () => {
  for (const change of [
    { base_url: 'http://127.0.0.1:10' }, { request_format: 'gemini' },
    { kind: 'local', request_format: 'ollama', local_backend: 'ollama' }, { api_key: 'new-key' },
  ]) {
    const h = editorHarness(); h.form = { ...h.form, ...change };
    h.state.bootstrap.settings = { new_conversation_model_key: 'online:a', new_conversation_model_name: 'chosen-a' };
    h.context.populateComposerModels = () => { throw new Error('draft check must not redraw the chat catalog'); };
    const checked = h.context.loadProviderModels();
    assert.equal(h.calls[0].options.body.model_key, undefined);
    for (const [key, value] of Object.entries(change)) assert.equal(h.calls[0].options.body[key], value);
    h.calls[0].resolve({ models: [{ id: 'draft-only-model' }] }); await checked;
    assert.equal(h.calls.length, 1); assert.equal(h.renderedModels[0].id, 'draft-only-model');
    assert.deepEqual(Object.keys(h.state.providerModelCatalogs).sort(), ['local:b', 'online:a']);
    assert.equal(h.state.providerModelCatalogs['online:a'][0].id, 'chosen-a');
    assert.equal(h.context.newConversationDefault().valid, true);
    assert.equal(h.elements['#composerModelSelect'].value, 'chosen-a');
    assert.match(h.elements['#providerError'].textContent, /模型目录可访问/);
  }
});
test('an unsaved connection response cannot replace a fresh shared catalog checked in parallel', async () => {
  const h = editorHarness(); h.form.base_url = 'http://127.0.0.1:10';
  const draft = h.context.loadProviderModels(); const saved = h.context.loadModelCatalog('online:a', { force: true });
  h.calls[1].resolve({ models: [{ id: 'fresh-saved-model' }] }); await saved;
  h.calls[0].resolve({ models: [{ id: 'draft-only-model' }] }); await draft;
  assert.equal(h.renderedModels[0].id, 'draft-only-model');
  assert.equal(h.state.providerModelCatalogs['online:a'][0].id, 'fresh-saved-model');
});
(async () => {
  let failed = 0;
  for (const [name, fn] of tests) { try { await fn(); console.log(`PASS ${name}`); } catch (e) { failed++; console.error(`FAIL ${name}: ${e.stack}`); } }
  console.log(`model selection races: ${tests.length - failed}/${tests.length} passed`); if (failed) process.exitCode = 1;
})();
