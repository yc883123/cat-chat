// Deterministic frontend checks for the writer setup single-step rerun entry:
// real source slices from 27-writer.js plus structural assertions on index.html.
// No browser, server, real model, clock sleeps or network calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const js = fs.readFileSync(path.join(root, 'public', 'js', '27-writer.js'), 'utf8').replace(/\r\n/g, '\n');
const html = fs.readFileSync(path.join(root, 'public', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
function fn(source, name) {
  const match = new RegExp(`^(?:export )?(?:async )?function ${name}\\(`, 'm').exec(source);
  assert(match, `missing owning function ${name}`);
  const end = source.indexOf('\n}', match.index);
  assert(end > match.index);
  return source.slice(match.index, end + 2).replace(/^export /, '');
}
function constOf(source, name) {
  const match = new RegExp(`^const ${name} = \\{`, 'm').exec(source);
  assert(match, `missing const ${name}`);
  const end = source.indexOf('\n};', match.index);
  assert(end > match.index);
  return source.slice(match.index, end + 3);
}
const cases = [];
function test(name, run) { cases.push({ name, run }); }

function element() {
  return { textContent: '', innerHTML: '', hidden: false, disabled: false, open: false, value: '', dataset: {}, style: {},
    children: [],
    replaceChildren(...nodes) { this.children = nodes; }, scrollIntoView() { this.scrolled = (this.scrolled || 0) + 1; },
    append() {}, listeners: {}, addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); } };
}
function harness({ preview = null, active = false, budgetOk = true } = {}) {
  const ids = ['writerStepRerunDialog', 'writerStepRerunTitle', 'writerStepRerunIntro', 'writerStepRerunSlots',
    'writerStepRerunChange', 'writerStepRerunWarn', 'writerStepRerunError', 'writerStepRerunStart',
    'writerStepRerunCancel', 'writerSetupNote', 'writerGenrePicker', 'writerTitle', 'writerBlurb',
    'writerWorldview', 'writerCharList'];
  const elements = Object.fromEntries(ids.map(id => [`#${id}`, element()]));
  const writerState = { bookId: 'B1', busy: false, charCount: 2, sideCount: 4, pendingSetupStep: '' };
  const h = { elements, writerState, calls: [], submits: [], opens: [], closes: [], notices: [], budgetChecks: [] };
  const context = {
    console, Math, Number, String, Promise, Object,
    writer: writerState,
    pipelineActive: () => active,
    readModel: () => ({ model_key: 'k1', model_name: 'model-a' }),
    PLAN_MODEL_FIELDS: { setup: ['setupProviderKey', 'setupModelName'] },
    STEP_LABELS: null, // patched below from the real source
    $: selector => elements[selector] || null,
    toast: message => h.notices.push(message),
    openDialog: selector => { h.opens.push(selector); elements[selector].open = true; },
    closeDialog: selector => { h.closes.push(selector); elements[selector].open = false; },
    api: async (url, options = {}) => {
      h.calls.push({ url, ...options });
      if (url === '/api/writer/generate/preview') {
        if (preview instanceof Error) throw preview;
        return preview;
      }
      return {};
    },
    gateByBudget: (model, budget) => { h.budgetChecks.push(budget); return budgetOk; },
    submitPipeline: async (kind, opts = {}) => { h.submits.push({ kind, ...opts }); return true; },
    document: { createElement: () => element() },
  };
  const stepLabels = vm.runInContext(`${constOf(js, 'STEP_LABELS')}\nSTEP_LABELS`, vm.createContext({}));
  context.STEP_LABELS = stepLabels;
  h.context = vm.createContext(context);
  vm.runInContext(`${constOf(js, 'SETUP_STEP_RERUN')}\n${fn(js, 'scrollSetupStepField')}\n${fn(js, 'openStepRerunDialog')}`, h.context);
  return h;
}
async function flush() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
const preview = slots => ({ kind: 'x', content: 'a'.repeat(120),
  budget: { estimated_tokens: 800, context_known: true, fits: true }, slots });

test('setup page has the single-step rerun bar and preview dialog', () => {
  const bar = html.match(/writer-step-rerun-bar[\s\S]*?<\/div>/)?.[0] || '';
  for (const step of ['genre', 'title', 'blurb', 'world', 'chars']) {
    assert.match(bar, new RegExp(`data-writer-step-rerun="${step}"`), step);
  }
  for (const id of ['writerStepRerunDialog', 'writerStepRerunTitle', 'writerStepRerunIntro', 'writerStepRerunSlots',
    'writerStepRerunChange', 'writerStepRerunWarn', 'writerStepRerunError', 'writerStepRerunStart', 'writerStepRerunCancel']) {
    assert.ok(html.includes(`id="${id}"`), id);
  }
  assert.ok(html.includes('补充要求（每一步都会带上）'), 'extra note copy matches real behavior');
  assert.ok(html.includes('「只重跑一步」先弹预览确认'), 'hint mentions the preview dialog');
});

test('submitPipeline sends setup_step with the step field and remembers it for the scroll-back', () => {
  assert.match(js, /const body = \{\n      kind,\n      step: step \|\| undefined,/);
  assert.match(js, /if \(kind === "setup_step" && step\) writer\.pendingSetupStep = step;/);
  assert.match(js, /fresh\.kind \|\| ""\) === "setup_step" && fresh\.status === "done" && writer\.pendingSetupStep/);
  for (const [step, target] of [['genre', '#writerGenrePicker'], ['title', '#writerTitle'], ['blurb', '#writerBlurb'],
    ['world', '#writerWorldview'], ['chars', '#writerCharList']]) {
    assert.ok(js.includes(`target: "${target}"`), `${step} scrolls to ${target}`);
  }
});

test('rerun dialog previews the chosen step and submits setup_step on confirm', async () => {
  const h = harness({ preview: preview([{ key: 'idea', label: '创作提示词与当前设定', chars: 120, included: true }]) });
  await h.context.openStepRerunDialog('world');
  await flush();
  const call = h.calls.find(row => row.url === '/api/writer/generate/preview');
  assert(call, 'preview requested');
  assert.equal(JSON.parse(JSON.stringify(call.body)).kind, 'world');
  assert.equal(JSON.parse(JSON.stringify(call.body)).book_id, 'B1');
  const el = h.elements;
  assert.equal(el['#writerStepRerunTitle'].textContent, '重跑「世界观设定」');
  const chips = el['#writerStepRerunSlots'].children || [];
  assert.ok(chips.some(chip => String(chip.textContent).includes('创作提示词与当前设定')), 'slot chips rendered');
  assert.ok(el['#writerStepRerunChange'].textContent.includes('世界观输入框'));
  assert.ok(el['#writerStepRerunWarn'].textContent.includes('设定版本'), 'world warns about overwrite fallback');
  assert.equal(el['#writerStepRerunStart'].disabled, false);
  el['#writerStepRerunStart'].onclick();
  assert.deepEqual(h.submits, [{ kind: 'setup_step', step: 'world' }]);
  assert.equal(el['#writerStepRerunDialog'].open, false, 'dialog closes on confirm');
});

test('title rerun only refreshes candidates and shows no overwrite warning', async () => {
  const h = harness({ preview: preview([]) });
  await h.context.openStepRerunDialog('title');
  await flush();
  const el = h.elements;
  assert.ok(el['#writerStepRerunChange'].textContent.includes('书名候选'));
  assert.ok(el['#writerStepRerunChange'].textContent.includes('保持不变'));
  assert.equal(el['#writerStepRerunWarn'].hidden, true);
  assert.equal(el['#writerStepRerunStart'].disabled, false);
});

test('rerun entry is blocked while a generation is already running', async () => {
  const h = harness({ active: true });
  await h.context.openStepRerunDialog('genre');
  await flush();
  assert.deepEqual(h.calls, [], 'no preview call');
  assert(h.notices.some(msg => msg.includes('正在进行的生成')));
  assert.deepEqual(h.opens, [], 'dialog never opened');
});

test('preview failure surfaces in the dialog instead of silently closing', async () => {
  const h = harness({ preview: new Error('网络断开') });
  await h.context.openStepRerunDialog('chars');
  await flush();
  assert.ok(h.elements['#writerStepRerunError'].textContent.includes('网络断开'));
  assert.equal(h.elements['#writerStepRerunDialog'].open, true, 'dialog stays open for retry');
});

test('budget gate keeps the dialog open and skips the submit', async () => {
  const h = harness({ preview: preview([]), budgetOk: false });
  await h.context.openStepRerunDialog('blurb');
  await flush();
  h.elements['#writerStepRerunStart'].onclick();
  await flush();
  assert.deepEqual(h.submits, []);
  assert.equal(h.budgetChecks.length, 1);
  assert.equal(h.elements['#writerStepRerunDialog'].open, true);
});

test('scrollSetupStepField targets the mapped field and tolerates unknown steps', () => {
  const h = harness();
  vm.runInContext(`scrollSetupStepField('chars'); scrollSetupStepField('');`, h.context);
  assert.equal(h.elements['#writerCharList'].scrolled, 1);
});

(async () => {
  for (const { name, run } of cases) await run();
  console.log(`writer step rerun frontend: ${cases.length} cases passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
