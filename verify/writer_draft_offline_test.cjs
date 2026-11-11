// Deterministic execution of the real writer module in a minimal VM (no browser/provider).
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'public', 'js', '27-writer.js'), 'utf8')
  .replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
const bodySource = fs.readFileSync(path.join(__dirname, '..', 'public/js/22b-body-drafts.js'), 'utf8')
  .replace(/^export /gm, '');
function installBodyDrafts(context) {
  vm.runInContext(bodySource + `
    const actualBodySession = createBodyDraftSession;
    createBodyDraftSession = (options) => actualBodySession({ ...options, backend: createMemoryDraftBackend() });
    createBodyDraftPanel = () => ({ status() {}, async refresh() {} });
  `, context);
}

function harness({ failure = '', hook = null, fits = true, dirty = true } = {}) {
  const calls = [], messages = [];
  const content = { value: '人工新稿：门后传来第三次敲门。', disabled: false };
  const note = { value: '保持克制' };
  const elements = { '#writerDraftContent': content, '#writerDraftNote': note };
  let stored = '服务端旧稿';
  const chapter = () => ({ no: 1, revision: 8, content: stored, title: '第一章', versions: [] });
  const book = () => ({ revision: 12, current_outline_version_id: 'outline-1', active_pipeline: null });
  let context;
  const api = async (url, options = {}) => {
    const method = options.method || 'GET';
    const key = `${method} ${url}`;
    calls.push({ key, body: options.body });
    if (hook) await hook(key, context);
    if ((failure === 'save' || failure === '409') && method === 'PATCH') {
      throw Object.assign(new Error('保存失败'), { status: failure === '409' ? 409 : 500 });
    }
    if (method === 'PATCH') {
      stored = options.body.content;
      return { chapter_no: 1, chapter_revision: 8, version: { content: stored } };
    }
    if (method === 'GET' && url.endsWith('/chapters/1')) {
      if (failure === 'chapter') throw new Error('章节刷新失败');
      if (failure === 'changed') return { ...chapter(), content: '别处刚修改的正文', revision: 9 };
      return chapter();
    }
    if (method === 'GET' && url === '/api/writer/books/book-a') {
      if (failure === 'index') throw new Error('索引刷新失败');
      return { book: book(), chapters: [chapter()] };
    }
    if (url.endsWith('/preview')) {
      if (failure === 'preview') throw new Error('预检失败');
      return { kind: options.body.kind, content: options.body.kind === 'chapter_continue' ? stored : '',
        budget: { context_known: true, fits, included: [], omitted: [] } };
    }
    if (url.endsWith('/pipelines')) return { pipeline: { status: 'done', kind: options.body.kind } };
    throw new Error(`Unexpected request: ${key}`);
  };
  context = vm.createContext({ console, Set, Map, Option: function () {},
    $: (selector) => elements[selector] || null, api, toast: (msg) => messages.push(msg),
    document: { querySelectorAll: () => [] }, window: { confirm: () => true },
    setTimeout, clearTimeout, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  });
  installBodyDrafts(context);
  vm.runInContext(source + `\n
    for (const name of []) {} // Keep execution strictly offline; no event binding/bootstrap.
    renderAll = renderDraft = renderPlan = renderBookSummary = renderChapters = renderPipeline = renderPageState = () => {};
    readModel = () => ({ model_key: 'offline', model_name: 'offline-model' });
    beginPipelineWatch = (pipeline) => { writer.pipeline = pipeline; };
    writer.bookId = 'book-a'; writer.book = { revision: 11, current_outline_version_id: 'outline-1' };
    writer.chapterNo = 1; writer.chapter = { no: 1, revision: 7, content: '服务端旧稿' };
    writer.stage = 'draft'; writer.draftDirty = ${dirty};
    globalThis.subject = { writer, submitPipeline, saveChapterEdit, openBook, openChapter, setStage, selectChapterVersion };
  `, context);
  return { context, ...context.subject, content, calls, messages };
}
const run = (h, kind = 'chapter_continue') => h.submitPipeline(kind, { chapterNo: 1 });
const submissions = (h) => h.calls.filter((row) => row.key.endsWith('/pipelines'));
const previews = (h) => h.calls.filter((row) => row.key.endsWith('/preview'));

(async () => {
  let cases = 0;
  for (const kind of ['chapter', 'chapter_segs', 'chapter_continue', 'chapter_rewrite']) {
    const h = harness();
    await run(h, kind);
    assert.deepEqual(h.calls.map((row) => row.key), [
      'PATCH /api/writer/books/book-a/chapters/1', 'GET /api/writer/books/book-a/chapters/1',
      'GET /api/writer/books/book-a', 'POST /api/writer/generate/preview', 'POST /api/writer/books/book-a/pipelines',
    ]);
    assert.equal(previews(h)[0].body.kind, kind);
    assert.equal(submissions(h)[0].body.kind, kind);
    assert.equal(submissions(h)[0].body.chapter_revision, 8);
    assert.equal(submissions(h)[0].body.revision, 12);
    assert.equal(previews(h)[0].body.extra_note, submissions(h)[0].body.extra_note);
    assert.equal(h.writer.draftDirty, false);
    assert.equal(h.writer.busy, false);
    cases++;
  }
  for (const failure of ['save', '409', 'chapter', 'index', 'changed', 'preview']) {
    const h = harness({ failure });
    await run(h);
    assert.equal(submissions(h).length, 0, failure);
    assert.equal(h.content.value, '人工新稿：门后传来第三次敲门。');
    if (failure !== 'preview') {
      assert.equal(h.writer.draftDirty, true, failure);
      assert.equal(previews(h).length, 0, failure);
    }
    assert.equal(h.writer.busy, false);
    cases++;
  }
  for (const point of ['PATCH', 'GET /api/writer/books/book-a/chapters/1', 'GET /api/writer/books/book-a', 'POST /api/writer/generate/preview']) {
    let changed = false;
    const h = harness({ hook: async (key, context) => {
      if (!changed && key.startsWith(point)) {
        changed = true;
        context.$('#writerDraftContent').value = '异步期间新输入';
        context.subject.writer.draftEditToken++;
        context.subject.writer.draftDirty = true;
      }
    } });
    await run(h);
    assert.equal(submissions(h).length, 0, point);
    assert.equal(h.writer.draftDirty, true);
    assert.equal(h.content.value, '异步期间新输入');
    cases++;
  }
  for (const identity of ['bookId', 'bookToken', 'chapterNo', 'chapterToken']) {
    const h = harness({ hook: async (key, context) => {
      if (key.startsWith('PATCH')) context.subject.writer[identity] = identity.endsWith('Id') ? 'book-b' : 99;
    } });
    await run(h);
    assert.equal(submissions(h).length, 0, identity);
    assert.equal(h.writer.chapter.revision, 7, 'late save must not update another target');
    assert.equal(h.writer.draftDirty, true);
    cases++;
  }
  {
    let release;
    const wait = new Promise((resolve) => { release = resolve; });
    const h = harness({ hook: async (key) => { if (key.startsWith('PATCH')) await wait; } });
    const first = run(h);
    await run(h, 'chapter_rewrite');
    assert.equal(await h.saveChapterEdit(), false);
    await h.openBook('book-b'); await h.openChapter(2); h.setStage('setup');
    await h.selectChapterVersion('old');
    assert.equal(h.writer.bookId, 'book-a');
    assert.equal(h.writer.chapterNo, 1);
    assert.equal(h.writer.stage, 'draft');
    assert.equal(h.calls.length, 1);
    release(); await first;
    assert.equal(submissions(h).length, 1);
    cases++;
  }
  {
    const h = harness({ fits: false }); await run(h);
    assert.equal(previews(h).length, 1); assert.equal(submissions(h).length, 0); cases++;
  }
  {
    const h = harness(); h.content.value = '  '; await run(h);
    assert.equal(h.calls.length, 0); assert.equal(h.writer.draftDirty, true); cases++;
  }
  {
    const h = harness({ dirty: false }); await run(h, 'chapter_rewrite');
    assert.equal(h.calls[0].key, 'POST /api/writer/generate/preview');
    assert.equal(submissions(h).length, 1); cases++;
  }
  for (const failure of ['', 'save', '409', 'chapter', 'index']) {
    const h = harness({ failure });
    assert.equal(await h.saveChapterEdit(), !failure, `save bool: ${failure}`);
    assert.equal(submissions(h).length, 0); cases++;
  }
  console.log(`writer draft offline: ${cases} cases passed (no browser, no provider)`);
  for (const withExtras of [false, true]) {
    for (const hasContent of [false, true]) {
      for (const mode of ['saved', 'download']) {
        const downloads = [], states = [];
        const context = vm.createContext({ console, Set, Map,
          api: async (_url, options) => {
            assert.equal(options.body.extras, withExtras ? '1' : '');
            return { path: 'fixture.md', suggested_name: '书.md', bytes: 100,
              has_content: hasContent, missing_chapters: [] };
          },
          saveLocalFile: (...args) => { downloads.push(args); return mode; },
          localFileUrl: (file) => file, toast() {},
        });
        installBodyDrafts(context);
        vm.runInContext(source + `\nsetSaveState = (...args) => globalThis.states.push(args);
          writer.bookId = 'book-a'; globalThis.subject = { exportBook };`, context);
        context.states = states;
        await context.subject.exportBook('md', withExtras);
        assert.equal(downloads.length, hasContent ? 1 : 0);
        assert.equal(states[0][0], hasContent ? '已导出' : withExtras ? '没有内容可导出' : '没有正文可导出');
        if (withExtras && !hasContent) assert.ok(!states[0][1].includes('改选'));
      }
    }
  }
  console.log('writer export offline: 8 cases passed (no browser, no provider)');
})().catch((error) => { console.error(error); process.exitCode = 1; });
