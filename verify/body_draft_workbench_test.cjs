const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const cp = require('node:child_process');
const root = path.resolve(__dirname, '..');
const strip = (text) => text.replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
const common = strip(fs.readFileSync(path.join(root, 'public/js/22b-body-drafts.js'), 'utf8'));
function harness(kind) {
  const editor = { value: '', focus() {} };
  const stored = new Map([[1, '第一章原文'], [2, '第二章原文']]);
  let revision = 1, hook = null;
  const chapter = (no) => kind === 'writer'
    ? { no, revision, title: `第${no}章`, content: stored.get(no), current_version_id: `v${revision}`, versions: [] }
    : { i: no, revision, len: stored.get(no).length, title: `第${no}章`, content: stored.get(no),
      base_manuscript_id: `o${no}`, content_manuscript_id: `o${no}`,
      manuscripts: [{ id: `o${no}`, kind: 'original', label: '原文' }, { id: `a${no}`, kind: 'manual', label: '改编稿' }] };
  const calls = [], choices = [], notices = [];
  const document = { activeElement: null, querySelectorAll: () => [] };
  const context = vm.createContext({ console, setTimeout, clearTimeout, document,
    window: { confirm: () => true }, localStorage: { setItem() {}, getItem() { return null; } },
    $: (selector) => /writerDraftContent|novelChapterBody/.test(selector) ? editor : null,
    toast: (value) => notices.push(value),
    api: async (url, opts = {}) => {
      calls.push([url, opts]);
      if (hook) await hook(url, opts);
      const no = Number(url.match(/chapters\/(\d+)/)?.[1] || 1);
      if (url.includes('/manuscripts/')) return { content: '另一份稿件' };
      if (opts.method === 'PATCH') {
        revision++;
        if (kind === 'writer') stored.set(no, opts.body.content);
        return kind === 'writer' ? { chapter_revision: revision }
          : { chapter: { ...chapter(no), created_manuscript: { label: '人工改编稿', len: opts.body.content.length } } };
      }
      if (url.includes('/chapters/')) return chapter(no);
      return { book: { revision, title: '测试', current_outline_version_id: 'outline' }, chapters: [chapter(1), chapter(2)] };
    },
  });
  vm.runInContext(common + `
    const actualSession = createBodyDraftSession;
    const memoryBackend = createMemoryDraftBackend();
    createBodyDraftSession = (options) => actualSession({ ...options, backend: memoryBackend });
    createBodyDraftPanel = () => ({ status() {}, async refresh() {} });
    chooseBodyDraft = async () => globalThis.choices.shift() || 'keep';
  `, context);
  context.choices = choices;
  const file = `public/js/${kind === 'writer' ? '27-writer' : '25-novel'}.js`;
  const source = process.env.BODY_DRAFT_BASELINE === '1'
    ? cp.execFileSync('git', ['show', `HEAD:${file}`], { cwd: root, encoding: 'utf8' })
    : fs.readFileSync(path.join(root, file), 'utf8');
  const init = kind === 'writer' ? `
    renderChapters = renderBookSummary = renderPageState = renderAll = () => {};
    renderDraft = () => { if (!writer.draftDirty) $('#writerDraftContent').value = writer.chapter?.content || ''; };
    refreshPlan = () => {};
    writer.bookId = 'book'; writer.chapter = ${JSON.stringify(chapter(1))}; writer.chapterNo = 1;
    writer.chapters = [writer.chapter];
    globalThis.subject = { state: writer, openChapter, openBook, save: saveChapterEdit,
      session: typeof writerBodyDrafts === 'undefined' ? null : writerBodyDrafts };
  ` : `
    renderAll = renderEditor = () => { $('#novelChapterBody').value = novel.editDraft; };
    renderManuscriptEditor = renderChapters = renderBookSummary = () => {};
    refreshChapterIndex = async () => {};
    novel.bookId = 'book'; novel.chapter = ${JSON.stringify(chapter(1))}; novel.chapterNo = 1;
    novel.chapters = [novel.chapter]; novel.book = { revision: 1 };
    globalThis.subject = { state: novel, openChapter, openBook, beginEdit, markDirty, save: saveManuscriptEdit,
      session: typeof novelBodyDrafts === 'undefined' ? null : novelBodyDrafts };
  `;
  vm.runInContext(strip(source) + init, context);
  function edit(text) {
    editor.value = text;
    if (kind === 'writer') vm.runInContext(`writer.draftDirty = true; writer.draftEditToken++;
      if (typeof captureWriterBody === 'function') captureWriterBody();`, context);
    else context.subject.markDirty({ target: editor });
  }
  return { ...context.subject, editor, edit, stored, calls, choices, notices,
    setHook(fn) { hook = fn; }, setRevision(value) { revision = value; } };
}
(async () => {
  let cases = 0;
  for (const kind of ['writer', 'novel']) {
    const h = harness(kind);
    if (kind === 'novel') await h.beginEdit('o1');
    h.edit('未保存的独有正文\n第二行');
    await h.openChapter(2);
    await h.openChapter(1);
    assert.equal(h.editor.value, '未保存的独有正文\n第二行', `${kind}: chapter switch must recover exact text`);
    assert.equal(h.calls.filter(([, o]) => o.method === 'PATCH').length, 0); cases++;
  }
  {
    const h = harness('novel'); await h.beginEdit('o1'); h.edit('原文上的编辑');
    await h.beginEdit('a1'); h.edit('改编稿上的编辑'); await h.beginEdit('o1');
    assert.equal(h.editor.value, '原文上的编辑');
    assert.equal((await h.session.list('novel', 'book')).length, 2); cases++;
  }
  for (const kind of ['writer', 'novel']) {
    const h = harness(kind); if (kind === 'novel') await h.beginEdit('o1');
    h.edit('切书之前的草稿');
    await h.openBook('another-book'); await h.openBook('book'); await h.openChapter(1);
    assert.equal(h.editor.value, '切书之前的草稿', `${kind}: book switch must preserve body`); cases++;
  }
  for (const kind of ['writer', 'novel']) {
    const h = harness(kind); if (kind === 'novel') await h.beginEdit('o1');
    h.edit('提交的正文');
    h.setHook(async (_url, opts) => { if (opts.method === 'PATCH') h.edit('保存期间的新正文'); });
    await h.save();
    assert.equal(h.editor.value, '保存期间的新正文');
    assert.ok((await h.session.list(kind, 'book')).some((row) => row.content === '保存期间的新正文'));
    await h.openChapter(2); await h.openChapter(1);
    assert.equal(h.editor.value, '保存期间的新正文'); cases++;
  }
  {
    const h = harness('writer'); h.edit('保留冲突草稿'); await h.openChapter(2);
    h.setRevision(2); h.choices.push('keep'); await h.openChapter(1);
    assert.equal(h.editor.value, '第一章原文');
    assert.equal((await h.session.list('writer', 'book'))[0].content, '保留冲突草稿');
    h.choices.push('continue'); await h.openChapter(1);
    assert.equal(h.editor.value, '保留冲突草稿'); cases++;
  }
  console.log(`body draft workbench: ${cases} cases passed`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
