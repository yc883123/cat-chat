// Deterministic frontend execution: real GOAL module and owning chat/UI functions.
// No browser, server, real model, clock sleeps or network calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, 'public', 'js', name), 'utf8').replace(/\r\n/g, '\n');
function fn(source, name) {
  const match = new RegExp(`^(?:export )?(?:async )?function ${name}\\(`, 'm').exec(source);
  assert(match, `missing owning function ${name}`);
  const end = source.indexOf('\n}', match.index);
  assert(end > match.index);
  return source.slice(match.index, end + 2).replace(/^export /, '');
}
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
async function flush() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
const goal = (id = 'A', status = 'running', version = 1, extra = {}) => ({
  id: `goal-${id}`, conversation_id: id, status, version, message: `target ${id}`,
  reason: '', generation: 2, run_id: `run-${id}`, created_at: 1795200000000,
  deadline_at: 1795210800000, model_requests: 3, tool_actions: 4, evidence: {}, ...extra,
});
function element() {
  return { value: '', hidden: false, disabled: false, dataset: {}, style: {}, innerHTML: '',
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute(name, value) { this[name] = value; },
    querySelector() { return null; }, querySelectorAll() { return []; }, contains() { return false; },
    replaceChildren() { this.innerHTML = ''; }, insertAdjacentHTML(_where, text) { this.innerHTML += text; },
    focus() { this.focused = true; }, click() { this.clicked = (this.clicked || 0) + 1; },
    getBoundingClientRect: () => ({ left: 20, right: 60, top: 400, bottom: 440 }),
    append() {}, listeners: {}, addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }, offsetWidth: 200, offsetHeight: 100,
  };
}
function harness() {
  const elements = Object.fromEntries(['messageInput', 'goalCard', 'sendButton', 'interjectButton',
    'attachButton', 'goalDraftChip', 'exitGoalDraft', 'composerForm', 'modelSelect', 'messages'].map(id => [`#${id}`, element()]));
  elements['#modelSelect'].value = 'provider';
  const state = { conversationId: 'A', pendingFiles: [], messages: [], conversations: [], chatBusy: false,
    runGeneration: 0, cancelledRunIds: new Set(), runReconnectTimers: new Set(), webSearchEnabled: false };
  const h = { elements, state, calls: [], notices: [], fetches: [], tasks: new Map(), getQueue: [], postQueue: [],
    defaultGoal: null, coarse: false, confirmed: true, uuid: 0, opened: [], events: [] };
  const document = { visibilityState: 'visible', activeElement: null,
    dispatchEvent(event) { h.events.push(event); if (event.type === 'naiba:goal-changed') context.updateContextComposerLock?.(state.chatBusy); },
    querySelector: selector => elements[selector] || null, querySelectorAll: () => [],
    createElement: () => element(), body: { append(node) { elements[`#${node.id}`] = node; } } };
  const context = { console, state, document, Map, Set, Promise, Date, AbortController, URLSearchParams,
    AbortSignal: { timeout: () => undefined }, CustomEvent: class { constructor(type, args = {}) { this.type = type; Object.assign(this, args); } },
    $: selector => elements[selector] || null, $$: () => [],
    escapeHtml: text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
    randomUuid: () => `request-${++h.uuid}`, toast: message => h.notices.push(message), confirm: () => h.confirmed,
    setTimeout: callback => { const id = h.tasks.size + 1; h.tasks.set(id, callback); return id; },
    clearTimeout: id => h.tasks.delete(id), window: { innerWidth: 375, innerHeight: 800 },
    api: async (url, options = {}) => {
      h.calls.push({ url, ...options });
      const queued = (options.method === 'POST' ? h.postQueue : h.getQueue).shift();
      if (queued instanceof Error) throw queued;
      if (queued) return await queued;
      return { goal: h.defaultGoal };
    },
    isCoarsePointer: () => h.coarse, resizeTextarea() {}, renderInputMirror() {},
    selectedProvider: () => ({}), composerModelIsValidated: () => true, composerModelChoice: () => 'model',
    conversationOpenGeneration: () => 1,
    isConversationOpenCurrent: (_generation, id) => id === state.conversationId,
    pendingConversationModelSave: () => null,
    createConversation: async () => { state.conversationId = 'new'; context.syncGoalConversation({ created: true }); return true; },
    attachmentChips: () => state.pendingFiles.filter(file => file.path && !file.folder),
    folderChips: () => state.pendingFiles.filter(file => file.folder), folderIndexMetadata: file => file,
    stripSkillReferences: text => String(text).replace('/skill', '').trim(),
    parseSkillReferences: text => text.includes('/skill') ? [{ skill: { id: 'skill-id' } }] : [],
    missingAttachmentPaths: async () => [], pendingContextWarning: () => null,
    openConversation: async id => { h.opened.push(id); return true; },
    messageElement: value => value, createRunRow: () => element(),
    setBusy: busy => { state.chatBusy = busy; },
    fetch: async (url, options) => { h.fetches.push({ url, ...options }); return { ok: true }; },
    consumeRunStream: async () => {}, finishRunSubscription: async () => { state.abortController = null; },
    attachmentPrecheckPending: false,
  };
  for (const name of ['renderPendingFiles', 'setStickToBottom', 'beginChoiceSubmit', 'commitChoiceSubmit', 'rollbackChoiceSubmit',
    'collapseComposerIfExpanded', 'hideSkillPopup', 'hideFilePopup', 'closeQuickMessagePanel', 'renderSidebar', 'notifyComposerChanged']) context[name] = () => {};
  vm.createContext(context);
  vm.runInContext(read('31-goals.js').replace(/^import .*\n/gm, '').replace(/^export /gm, ''), context);
  for (const [file, names] of [
    ['03-media.js', ['updateContextComposerLock', 'updateSendButtonState', 'updateInterjectButtonState']],
    ['11-run-stream.js', ['sendChatMessage']], ['12-chat-input.js', ['cancelCurrentRun']],
    ['15-bind-events.js', ['openFilePicker', 'ensureAttachMenu', 'hideAttachMenu', 'showAttachMenu']],
    ['04-messages.js', ['startEditMessage', 'resendFromUserMessage', 'regenerateMessage', 'deleteMessageFlow', 'startNewSession', 'cancelSessionStart', 'cancelCompaction']],
    ['18-interjections.js', ['sendRunInterjection', 'guideInterjection', 'guideAllInterjections', 'deleteInterjection', 'reuseInterjection']],
  ]) vm.runInContext(names.map(name => fn(read(file), name)).join('\n'), context);
  context.ATTACH_ACCEPT = { media: 'image/*,video/*', file: 'application/pdf,.txt' };
  h.context = context;
  h.navigate = async (id, created = false) => { state.conversationId = id; context.syncGoalConversation({ created }); await flush(); };
  return h;
}
const cases = [];
function test(name, run) { cases.push({ name, run }); }

test('GET restores card, escapes data and locks all nonterminal statuses', async () => {
  for (const status of ['queued', 'running', 'pausing', 'paused', 'needs_user', 'blocked', 'awaiting_review', 'cancelling', 'cancelled', 'completed']) {
    const h = harness(); h.defaultGoal = goal('A', status, 7, { reason: '<bad>', evidence: { artifact: '<script>' } });
    await h.navigate('A');
    assert.equal(h.context.goalBlocksChat(), !['cancelled', 'completed'].includes(status), status);
    assert.equal(h.elements['#goalCard'].hidden, false);
    assert(h.elements['#goalCard'].innerHTML.includes('&lt;bad&gt;'));
    assert(!h.elements['#goalCard'].innerHTML.includes('<script>'));
    assert(h.elements['#goalCard'].innerHTML.includes('模型请求 3 次'));
    assert.equal(h.elements['#interjectButton'].hidden, true);
  }
});

test('GOAL chip exit, conversation drafts and files remain isolated', async () => {
  const h = harness(); await h.navigate('A');
  h.elements['#messageInput'].value = 'draft A'; h.state.pendingFiles = [{ path: 'a.txt' }];
  assert(h.context.setGoalDraftMode(true)); assert(h.context.setGoalDraftMode(false));
  assert.equal(h.elements['#messageInput'].value, 'draft A'); assert.equal(h.state.pendingFiles.length, 1);
  h.context.setGoalDraftMode(true); await h.navigate('B');
  assert.equal(h.context.goalDraftEnabled(), false); assert.equal(h.elements['#messageInput'].value, '');
  h.elements['#messageInput'].value = 'draft B'; await h.navigate('A');
  assert.equal(h.elements['#messageInput'].value, 'draft A'); assert(h.context.goalDraftEnabled());
  assert.equal(h.state.pendingFiles[0].path, 'a.txt'); await h.navigate('B');
  assert.equal(h.elements['#messageInput'].value, 'draft B'); assert.equal(h.state.pendingFiles.length, 0);
});

test('late A reads cannot paint B or a later A navigation', async () => {
  const h = harness(), gate = deferred(); h.getQueue.push(gate.promise);
  h.context.syncGoalConversation(); await h.navigate('B');
  h.defaultGoal = goal('A', 'paused', 8); await h.navigate('A');
  gate.resolve({ goal: goal('A', 'running', 2) }); await flush();
  assert(h.elements['#goalCard'].innerHTML.includes('已暂停'));
  await h.navigate('B'); assert.equal(h.elements['#goalCard'].hidden, false); // cross-conversation payload rejected visibly
  assert(!h.elements['#goalCard'].innerHTML.includes('target A'));
});

test('read failure stays locked and visible; 404 older server preserves ordinary chat', async () => {
  const h = harness(); h.getQueue.push(new Error('offline')); await h.navigate('A');
  assert(h.context.goalBlocksChat()); assert(h.elements['#goalCard'].innerHTML.includes('重试'));
  await h.context.refreshGoal(); assert.equal(h.context.goalBlocksChat(), false);
  h.getQueue.push(Object.assign(new Error('not found'), { status: 404 })); await h.navigate('old');
  assert.equal(h.context.goalBlocksChat(), false);
  h.getQueue.push(new Error('offline again')); await h.context.refreshGoal(); assert(h.context.goalBlocksChat());
});

test('failed create retains draft and request ID; double click submits only once', async () => {
  const h = harness(); await h.navigate('A'); h.context.setGoalDraftMode(true);
  h.elements['#messageInput'].value = 'target'; h.state.pendingFiles = [{ path: 'a' }];
  const payload = { conversation_id: 'A', message: 'target', attachments: [{ path: 'a' }] };
  const gate = deferred(); h.postQueue.push(gate.promise);
  const first = h.context.submitGoal(payload); assert.equal(await h.context.submitGoal(payload), false);
  gate.reject(new Error('lost response')); assert.equal(await first, false);
  assert.equal(h.elements['#messageInput'].value, 'target'); assert.equal(h.state.pendingFiles.length, 1); assert(h.context.goalDraftEnabled());
  h.postQueue.push({ goal: goal() }); assert.equal(await h.context.submitGoal(payload), true);
  const posts = h.calls.filter(call => call.method === 'POST'); assert.equal(posts.length, 2);
  assert.equal(posts[0].body.request_id, posts[1].body.request_id);
  assert.equal(h.elements['#messageInput'].value, ''); assert.equal(h.state.pendingFiles.length, 0); assert(!h.context.goalDraftEnabled());
});

test('changed submission gets a fresh ID and malformed response preserves draft', async () => {
  const h = harness(); await h.navigate('A'); h.context.setGoalDraftMode(true); h.elements['#messageInput'].value = 'one';
  h.postQueue.push({ goal: goal('B') }); await h.context.submitGoal({ conversation_id: 'A', message: 'one' });
  assert.equal(h.elements['#messageInput'].value, 'one');
  h.postQueue.push(new Error('offline')); await h.context.submitGoal({ conversation_id: 'A', message: 'two' });
  const posts = h.calls.filter(call => call.method === 'POST'); assert.notEqual(posts[0].body.request_id, posts[1].body.request_id);
});

test('create completing after navigation only clears originating draft', async () => {
  const h = harness(); await h.navigate('A'); h.context.setGoalDraftMode(true); h.elements['#messageInput'].value = 'target A';
  h.state.pendingFiles = [{ path: 'a' }]; const gate = deferred(); h.postQueue.push(gate.promise);
  const sent = h.context.submitGoal({ conversation_id: 'A', message: 'target A' }); await h.navigate('B');
  h.elements['#messageInput'].value = 'unsent B'; gate.resolve({ goal: goal() }); assert(await sent);
  assert.equal(h.elements['#messageInput'].value, 'unsent B');
  h.defaultGoal = goal(); await h.navigate('A'); assert.equal(h.elements['#messageInput'].value, ''); assert(!h.context.goalDraftEnabled());
});

test('controls send version, preserve locks on failure and refresh after conflicts', async () => {
  const h = harness(); h.defaultGoal = goal('A', 'running', 9); await h.navigate('A');
  h.postQueue.push(Object.assign(new Error('conflict'), { status: 409 })); h.defaultGoal = goal('A', 'paused', 11);
  assert.equal(await h.context.changeGoal('pause'), false); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.find(call => call.method === 'POST').body)), { version: 9 });
  assert(h.context.goalBlocksChat()); assert(h.elements['#goalCard'].innerHTML.includes('已暂停'));
  h.confirmed = false; assert.equal(await h.context.changeGoal('cancel'), false);
  h.postQueue.push({ goal: goal('A', 'queued', 12) }); h.defaultGoal = goal('A', 'queued', 12);
  assert(await h.context.changeGoal('resume')); await flush();
  assert(h.context.goalCanPause());
});

test('pause uses GOAL endpoint even between Runs, never chat cancel', async () => {
  const h = harness(); h.defaultGoal = goal(); await h.navigate('A'); h.postQueue.push({ goal: goal('A', 'pausing', 2) });
  await h.context.cancelCurrentRun();
  assert(h.calls.some(call => call.url === '/api/goals/goal-A/pause'));
  assert(!h.calls.some(call => call.url === '/api/chat/cancel'));
});

test('pre-write GET cannot roll back control result; older versions ignored', async () => {
  const h = harness(); h.defaultGoal = goal(); await h.navigate('A');
  const readGate = deferred(), writeGate = deferred(); h.getQueue.push(readGate.promise); const reading = h.context.refreshGoal();
  h.postQueue.push(writeGate.promise); const writing = h.context.changeGoal('pause');
  h.defaultGoal = goal('A', 'paused', 5); writeGate.resolve({ goal: goal('A', 'paused', 5) }); await writing; await flush();
  readGate.resolve({ goal: goal('A', 'running', 1) }); await reading;
  assert(h.elements['#goalCard'].innerHTML.includes('已暂停'));
  h.getQueue.push({ goal: goal('A', 'running', 2) }); await h.context.refreshGoal();
  assert(h.elements['#goalCard'].innerHTML.includes('已暂停'));
});

test('hidden document stops polling, visibility resume refreshes', async () => {
  const h = harness(); await h.navigate('A'); const count = h.calls.length;
  h.context.document.visibilityState = 'hidden'; await h.context.refreshGoal(); assert.equal(h.calls.length, count);
  h.context.document.visibilityState = 'visible'; h.defaultGoal = goal(); await h.context.refreshGoal(); assert(h.context.goalIsActive());
});

test('desktop/mobile menus keep picker types and disable GOAL while editing/busy', async () => {
  const h = harness(); await h.navigate('A'); h.context.showAttachMenu(h.elements['#attachButton']);
  let menu = h.elements['#attachMenu']; assert(menu.innerHTML.includes('>附件<')); assert(menu.innerHTML.includes('>GOAL<'));
  assert(!menu.innerHTML.includes('照片')); assert.equal(h.elements['#attachButton']['aria-expanded'], 'true');
  h.context.hideAttachMenu(); assert.equal(h.elements['#attachButton']['aria-expanded'], 'false');
  h.coarse = true; h.state.editingMessageId = 'edit'; h.context.showAttachMenu(h.elements['#attachButton']);
  assert(menu.innerHTML.includes('照片 / 视频')); assert(menu.innerHTML.includes('文件（')); assert(menu.innerHTML.includes('disabled'));
  h.elements['#fileInput'] = element(); h.context.openFilePicker('media'); assert.equal(h.elements['#fileInput'].accept, 'image/*,video/*');
  h.context.openFilePicker(''); assert.equal(h.elements['#fileInput'].accept, ''); assert.equal(h.elements['#fileInput'].clicked, 2);
});

test('normal chat still posts original chat payload; GOAL only posts goals payload', async () => {
  for (const goalMode of [false, true]) {
    const h = harness(); await h.navigate('A'); h.elements['#messageInput'].value = '/skill target';
    h.state.pendingFiles = [{ path: 'input.txt', name: 'input.txt', size: 2, thumb_path: 'thumb' }, { path: 'dir', name: 'dir', folder: true }];
    if (goalMode) { h.context.setGoalDraftMode(true); h.postQueue.push({ goal: goal() }); }
    await h.context.sendChatMessage();
    const payload = goalMode ? h.calls.find(call => call.url === '/api/goals').body : JSON.parse(h.fetches[0].body);
    assert.equal(payload.message, 'target'); assert.equal(payload.display_message, '/skill target'); assert.equal(payload.model_name, 'model');
    assert.equal(payload.skill_policy.referenced_ids[0], 'skill-id'); assert.equal(payload.attachments[0].thumb_path, 'thumb'); assert.equal(payload.folders[0].path, 'dir');
    if (goalMode) { assert.equal(h.fetches.length, 0); assert(payload.request_id); assert.equal(h.opened[0], 'A'); }
    else { assert.equal(h.fetches[0].url, '/api/chat'); assert(!Object.hasOwn(payload, 'request_id')); }
  }
});

test('GOAL rejects reference/attachment-only input; ordinary attachment-only send remains valid', async () => {
  for (const goalMode of [false, true]) {
    const h = harness(); await h.navigate('A'); h.state.pendingFiles = [{ path: 'input.txt' }];
    h.elements['#messageInput'].value = goalMode ? '/skill' : ''; if (goalMode) h.context.setGoalDraftMode(true);
    await h.context.sendChatMessage(); assert.equal(h.fetches.length, goalMode ? 0 : 1);
    assert(!h.calls.some(call => call.url === '/api/goals'));
    if (goalMode) assert.equal(h.state.pendingFiles.length, 1);
  }
});

test('late attachment precheck cannot submit a goal into another conversation', async () => {
  const h = harness(); await h.navigate('A'); h.context.setGoalDraftMode(true); h.elements['#messageInput'].value = 'target'; h.state.pendingFiles = [{ path: 'a' }];
  const gate = deferred(); h.context.missingAttachmentPaths = () => gate.promise;
  const sending = h.context.sendChatMessage(); await flush(); await h.navigate('B'); h.elements['#messageInput'].value = 'draft B';
  gate.resolve([]); await sending; assert(!h.calls.some(call => call.url === '/api/goals')); assert.equal(h.elements['#messageInput'].value, 'draft B');
});

test('active target rejects chat/interjection/history mutation at owning functions', async () => {
  const h = harness(); h.defaultGoal = goal('A', 'paused'); await h.navigate('A');
  const before = h.calls.length;
  for (const name of ['sendChatMessage', 'startEditMessage', 'resendFromUserMessage', 'regenerateMessage', 'deleteMessageFlow',
    'startNewSession', 'cancelSessionStart', 'cancelCompaction', 'sendRunInterjection', 'guideInterjection', 'guideAllInterjections', 'deleteInterjection', 'reuseInterjection']) {
    await h.context[name]('message');
  }
  assert.equal(h.calls.length, before); assert.equal(h.fetches.length, 0);
});

test('returning to a cached idle conversation still checks for goals created elsewhere', async () => {
  const h = harness(); await h.navigate('A'); await h.navigate('B');
  const gate = deferred(); h.getQueue.push(gate.promise); h.state.conversationId = 'A'; h.context.syncGoalConversation();
  assert(h.context.goalBlocksChat(), 'cached null must not unlock before fresh GET');
  gate.resolve({ goal: goal() }); await flush(); assert(h.context.goalIsActive());
});

test('new conversation preserves blank-page GOAL draft, explicit navigation does not inherit mode', async () => {
  const h = harness(); await h.navigate(''); h.context.setGoalDraftMode(true);
  h.elements['#messageInput'].value = 'new goal'; await h.context.sendChatMessage();
  const post = h.calls.find(call => call.url === '/api/goals'); assert(post); assert.equal(post.body.conversation_id, 'new');
  // The default fixture returns no goal, so failure must keep its original draft.
  assert(h.context.goalDraftEnabled()); assert.equal(h.elements['#messageInput'].value, 'new goal');
  await h.navigate('other', true); assert(!h.context.goalDraftEnabled()); assert.equal(h.elements['#messageInput'].value, '');
});

test('accept/cancel are status-gated, versioned, and release the chat lock only after terminal response', async () => {
  for (const [action, status, final] of [['accept', 'awaiting_review', 'completed'], ['cancel', 'blocked', 'cancelled']]) {
    const h = harness(); h.defaultGoal = goal('A', status, 4); await h.navigate('A');
    const gate = deferred(); h.postQueue.push(gate.promise); const changing = h.context.changeGoal(action);
    assert(h.context.goalBlocksChat()); h.defaultGoal = goal('A', final, 5); gate.resolve({ goal: h.defaultGoal });
    assert(await changing); await flush(); assert(!h.context.goalBlocksChat());
    const call = h.calls.find(call => call.method === 'POST'); assert.equal(call.body.version, 4); assert(call.url.endsWith(`/${action}`));
    assert.equal(await h.context.changeGoal(action), false);
  }
});

test('failed GOAL send through real chat function leaves references and attachments intact', async () => {
  const h = harness(); await h.navigate('A'); h.context.setGoalDraftMode(true);
  h.elements['#messageInput'].value = '/skill target'; h.state.pendingFiles = [{ path: 'a' }];
  h.postQueue.push(new Error('offline')); await h.context.sendChatMessage();
  const first = h.calls.find(call => call.url === '/api/goals').body.request_id;
  assert.equal(h.elements['#messageInput'].value, '/skill target'); assert.equal(h.state.pendingFiles.length, 1);
  h.postQueue.push({ goal: goal() }); await h.context.sendChatMessage();
  assert.equal(h.calls.filter(call => call.url === '/api/goals')[1].body.request_id, first);
});

test('late missing-file response does not remove matching files from another draft', async () => {
  const h = harness(); await h.navigate('A'); h.context.setGoalDraftMode(true); h.elements['#messageInput'].value = 'target'; h.state.pendingFiles = [{ path: 'same' }];
  const gate = deferred(); h.context.missingAttachmentPaths = () => gate.promise;
  const sending = h.context.sendChatMessage(); await flush(); await h.navigate('B'); h.state.pendingFiles = [{ path: 'same' }];
  gate.resolve(['same']); await sending; assert.equal(h.state.pendingFiles.length, 1);
});

test('real add-menu and submit event handlers select mode and preserve synchronous file activation', async () => {
  const h = harness(); await h.navigate('A'); const source = read('15-bind-events.js');
  const events = {};
  h.context.document.addEventListener = (type, handler) => { (events[type] ||= []).push(handler); };
  h.context.window.addEventListener = () => {};
  h.context.bindOnebotSettingsEvents = () => {};
  // Execute the actual event registration blocks, without booting unrelated settings/workbench code.
  const begin = source.indexOf("  // 主对话添加菜单：");
  const end = source.indexOf('  // 文件动作条在手机上的', begin);
  vm.runInContext(source.slice(begin, end), h.context);
  const submitStart = source.indexOf("  $('#composerForm').addEventListener('submit'");
  const submitEnd = source.indexOf('  // 「插话」按钮', submitStart);
  vm.runInContext(source.slice(submitStart, submitEnd), h.context);
  h.context.ensureAttachMenu();
  let kind = 'goal';
  const item = { getAttribute: () => kind };
  const event = { target: { closest: selector => selector === '#attachMenu [data-attach-kind]' ? item : null }, preventDefault() {} };
  h.elements['#messageInput'].value = 'draft';
  events.click[0](event); assert(h.context.goalDraftEnabled()); assert.equal(h.elements['#messageInput'].value, 'draft');
  h.elements['#fileInput'] = element(); kind = 'media'; events.click[0](event);
  assert.equal(h.elements['#fileInput'].clicked, 1); assert(h.context.goalDraftEnabled());
  kind = ''; events.click[0](event); assert.equal(h.elements['#fileInput'].accept, '');
  let paused = 0, ordinary = 0;
  h.context.cancelCurrentRun = () => { paused++; }; h.context.sendMessage = () => { ordinary++; };
  const submit = h.elements['#composerForm'].listeners.submit[0];
  submit({ preventDefault() {} }); assert.equal(ordinary, 1);
  h.defaultGoal = goal(); await h.context.refreshGoal(); submit({ preventDefault() {} }); assert.equal(paused, 1);
});

test('attachment edits during GOAL precheck do not send a stale attachment snapshot', async () => {
  const h = harness(); await h.navigate('A'); h.context.setGoalDraftMode(true); h.elements['#messageInput'].value = 'target';
  h.state.pendingFiles = [{ path: 'a' }]; const gate = deferred(); h.context.missingAttachmentPaths = () => gate.promise;
  const sending = h.context.sendChatMessage(); await flush(); h.state.pendingFiles.push({ path: 'b' }); gate.resolve([]); await sending;
  assert(!h.calls.some(call => call.url === '/api/goals')); assert.equal(h.state.pendingFiles.length, 2);
});

test('editing/regenerating history exits unsent GOAL mode before ordinary resend', async () => {
  const h = harness(); await h.navigate('A'); h.context.setGoalDraftMode(true); h.elements['#messageInput'].value = 'unsent target';
  assert.equal(await h.context.resendFromUserMessage('', ''), false);
  assert(!h.context.goalDraftEnabled()); assert.equal(h.elements['#messageInput'].value, 'unsent target');
  assert(!h.calls.some(call => call.url === '/api/goals'));
});

test('waiting_tool keeps goal controls and pauses through the goal endpoint', async () => {
  const h = harness(); h.defaultGoal = goal('A', 'waiting_tool', 3); await h.navigate('A');
  assert(h.context.goalBlocksChat());
  const html = h.elements['#goalCard'].innerHTML;
  assert(html.includes('等待后台任务')); assert(html.includes('查看轮次记录')); assert(html.includes('追加额度或时限'));
  h.postQueue.push({ goal: goal('A', 'pausing', 4) });
  await h.context.changeGoal('pause');
  assert(h.calls.some(call => call.url === '/api/goals/goal-A/pause'));
});

test('extend form posts versioned idempotent increments and keeps unsubmitted values', async () => {
  const h = harness(); const fields = {};
  h.elements['#goalCard'].querySelector = (selector) => {
    const match = /data-goal-field="([^"]+)"/.exec(String(selector || ''));
    if (!match) return null;
    fields[match[1]] ||= element();
    return fields[match[1]];
  };
  const limits = { model_requests: 256, tool_actions: 200, rounds: 32 };
  h.defaultGoal = goal('A', 'blocked', 6, { limits, reason: '额度', stop_code: 'budget_model' }); await h.navigate('A');
  const html = h.elements['#goalCard'].innerHTML;
  assert(html.includes('模型请求 3/256 次')); assert(html.includes('第 2/32 轮')); assert(html.includes('额度用尽'));
  fields['extend-model'] = element(); fields['extend-minutes'] = element();
  fields['extend-model'].value = '32'; fields['extend-minutes'].value = '60';
  h.postQueue.push(new Error('lost response'));
  assert.equal(await h.context.submitGoalExtend(), false);
  h.postQueue.push({ goal: goal('A', 'blocked', 7, { limits: { ...limits, model_requests: 288 } }) });
  assert(await h.context.submitGoalExtend());
  const posts = h.calls.filter(call => String(call.url).endsWith('/extend'));
  assert.equal(posts.length, 2);
  assert.equal(posts[0].body.request_id, posts[1].body.request_id);
  assert.deepEqual(JSON.parse(JSON.stringify(posts[1].body.increments)), { model_requests: 32 });
  assert.equal(posts[1].body.extra_minutes, 60);
  assert.equal(posts[1].body.version, 6);
  fields['extend-model'].value = '16'; fields['extend-minutes'].value = '';
  h.postQueue.push({ goal: goal('A', 'blocked', 8, { limits }) });
  assert(await h.context.submitGoalExtend());
  const third = h.calls.filter(call => String(call.url).endsWith('/extend'))[2];
  assert.notEqual(third.body.request_id, posts[0].body.request_id);
  assert.deepEqual(JSON.parse(JSON.stringify(third.body.increments)), { model_requests: 16 });
  assert.equal(third.body.extra_minutes, 0);
});

test('rounds pager accumulates pages and sends the scoped cursor', async () => {
  const h = harness(); h.defaultGoal = goal('A', 'running', 9); await h.navigate('A');
  h.getQueue.push({ rounds: [{ generation: 3, status: 'completed', reason: 'ok', model_requests_start: 5,
    model_requests_end: 8, tool_actions_start: 2, tool_actions_end: 4, created_at: 1795200000000,
    finished_at: 1795200001000, evidence: [] }], next_cursor: 'abc', has_more: true });
  assert(await h.context.loadGoalRounds());
  let html = h.elements['#goalCard'].innerHTML;
  assert(html.includes('第 3 轮')); assert(html.includes('模型请求 +3')); assert(html.includes('更多轮次'));
  h.getQueue.push({ rounds: [{ generation: 2, status: 'blocked', reason: 'stop', evidence: [] }], next_cursor: '', has_more: false });
  assert(await h.context.loadGoalRounds());
  const second = String(h.calls.filter(call => String(call.url).includes('/rounds'))[1].url);
  assert(second.includes('cursor=abc')); assert(second.includes('page_size=10'));
  html = h.elements['#goalCard'].innerHTML;
  assert(html.includes('第 2 轮')); assert(!html.includes('更多轮次'));
});

(async () => {
  for (const { name, run } of cases) { await run(); console.log(`PASS ${name}`); }
  console.log(`goal frontend: ${cases.length} cases passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
