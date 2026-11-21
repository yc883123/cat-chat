// Writer intervention regression on real markup/CSS/module with the isolated decision fixture.
// Run: NAIBA_SMOKE_BASE=http://127.0.0.1:<fixture-port> node verify/writer_decision_smoke.cjs
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const base = process.env.NAIBA_SMOKE_BASE;
if (!base || !/^http:\/\/127\.0\.0\.1:\d+\/?$/.test(base)) throw new Error('Set NAIBA_SMOKE_BASE to the loopback --decisions fixture URL');
const url = base.replace(/\/$/, '');
const viewports = [{ width: 1280, height: 800 }, { width: 375, height: 812 }];

async function checkWriter(browser, viewport) {
  const page = await browser.newPage({ viewport });
  const mobile = viewport.width === 375;
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const $ = (selector) => page.locator(selector);
  const visible = (selector) => $(selector).waitFor({ state: 'visible' });
  const hidden = (selector) => $(selector).waitFor({ state: 'hidden' });
  const text = async (selector, expected) => assert.match(await $(selector).innerText(), expected);
  const pending = '#writerPipelinePending';
  const supplement = '#writerPipelineDecisionSupplement';
  const raw = '#writerPipelinePendingRaw';
  const editor = '#writerPipelinePendingContent';
  const error = '#writerPipelineDecisionError';
  const history = '#writerPipelineHistoryList li';
  const control = (body) => page.request.post(`${url}/__fixture/control`, { data: body });
  const status = async () => (await page.request.get(`${url}/__fixture/status`)).json();
  const decisions = async () => (await status()).requests.filter(({ path }) => path.endsWith('/pipeline/decision'));
  const stage = (name) => $(`[data-writer-stage="${name}"]`).click();
  const seed = async (step) => {
    assert.ok((await control({ step })).ok());
    await $('#fixtureFirstBook').click();
  };
  const openNotes = async () => {
    if (mobile && !(await $(pending).isVisible())) await $('#writerAsideBtn').click();
    await visible(pending);
  };
  const title = (expected) => page.waitForFunction((value) => document.querySelector('#writerPipelinePendingTitle').textContent.includes(value), expected);
  const waitRequest = (path) => page.waitForRequest((req) => req.method() === 'POST' && new URL(req.url()).pathname.endsWith(path));
  const switchBook = async (selector, name) => {
    await $(selector).click();
    await page.waitForFunction((value) => document.querySelector('#writerPageState').textContent.includes(value), name);
  };
  const confirmNext = () => page.once('dialog', (dialog) => dialog.accept());
  try {
    assert.ok((await control({ reset: true })).ok());
    const setup = await status();
    const firstBook = setup.book_ids[0];
    await page.goto(url);
    await page.waitForFunction(() => document.querySelector('#writerPageState').textContent.includes('离线正文验收'));
    if (mobile) {
      await visible('#writerAsideBtn');
      await text('#writerAsideBtn', /处理生成结果/);
      assert.equal(await $(pending).isVisible(), false);
    }
    await openNotes();
    await visible(editor);
    assert.equal(await $(editor).inputValue(), '');
    await text('#writerPipelineBodySaved', /本轮正文未保存/);
    await text('#writerPipelineMissingList', /补完正文结尾/);
    await hidden('#writerPipelineDecisionAccept');
    await hidden(raw);
    await hidden('#writerPipelineError');
    await hidden('#writerPipelineStream');
    assert.equal(await $(pending).count(), 1);

    // A blur, pane/stage navigation, book switch, and action error leave a draft alone.
    const edited = '手工补完。\n门外传来第三次敲门。';
    await $(editor).fill(edited);
    await $(editor).blur();
    await stage('outline');
    if (mobile) {
      await $('#writerAsideClose').click();
      await openNotes();
    }
    assert.equal(await $(editor).inputValue(), edited);
    await switchBook('#fixtureOtherBook', '第二本隔离作品');
    await title('题材推断');
    await openNotes();
    await text('#writerPipelineMissingList', /题材大类/);
    await $(`${raw} summary`).click();
    await text('#writerPipelinePendingRawText', /<img id="model-injection"/);
    assert.equal(await $('#model-injection').count(), 0);
    assert.equal(await $(pending).getByLabel('基调', { exact: true }).inputValue(), '冷峻');
    await switchBook('#fixtureFirstBook', '离线正文验收');
    await title('正文');
    assert.equal(await $(editor).inputValue(), edited);

    await control({ scenario: 'decision_invalid' });
    await $(supplement).click();
    await visible(error);
    await text(error, /未通过校验/);
    assert.equal(await $(editor).inputValue(), edited);
    const rejected = (await decisions())[0].body;
    assert.equal(rejected.content, edited);

    // Server commits but the transport loses the first response. Exact retry uses the
    // same key, writes the body once, and does not duplicate its history receipt.
    await control({ scenario: 'decision_drop' });
    await $(supplement).click();
    await visible(error);
    await text(error, /未能确认处理结果/);
    await $(supplement).click();
    await title('章末状态');
    const attempts = (await decisions()).map((row) => row.body);
    assert.equal(attempts.length, 2);
    assert.deepEqual(attempts[0], attempts[1]);
    assert.notEqual(attempts[0].request_key, rejected.request_key);
    assert.equal(attempts[0].step, 'draft');
    for (const key of ['pipeline_id', 'step_index', 'attempt', 'decision_revision']) assert.ok(Object.hasOwn(attempts[0], key));
    await text('#writerPipelineBodySaved', /正文已保存/);
    assert.equal(await $(history).count(), 1);
    await text(history, /正文：已补充/);

    // Starting a new job or requesting an overwrite cannot erase an unresolved step.
    await stage('chapter');
    if (mobile) await $('#writerAsideClose').click();
    await $('#writerSplitCount').fill('3');
    await $('#writerSplitStart').click();
    await visible(pending);
    await hidden('#writerSplitDialog');
    assert.equal((await status()).requests.filter(({ path }) => /\/(pipelines|clear)$/.test(path)).length, 0);

    await $('#writerPipelineDecisionSkip').click();
    await text('#writerPipelinePendingConsequence', /已保存的正文不会改动/);
    await $('#writerPipelineSkipSubmit').click();
    await text(error, /填写跳过原因/);
    await $('#writerPipelineSkipReason').fill('正文已核对，本轮不提取状态');
    await $('#writerPipelineSkipSubmit').click();
    assert.equal((await decisions()).length, 2);
    await $('#writerPipelineSkipConfirmed').check();
    await $('#writerPipelineSkipSubmit').click();
    await hidden(pending);
    const skip = (await decisions()).at(-1).body;
    assert.equal(skip.confirmed, true);
    assert.equal(skip.reason, '正文已核对，本轮不提取状态');
    assert.notEqual(skip.request_key, attempts[0].request_key);
    assert.equal(await $(history).count(), 2);
    await text(`${history}:last-child`, /章末状态：已跳过/);
    await text(`${history}:last-child`, /已保存的正文不会改动/);
    const chapter = await (await page.request.get(`${url}/api/writer/books/${firstBook}/chapters/2`)).json();
    assert.equal(chapter.content, edited);
    assert.equal(chapter.versions.length, 2);

    // Overwrite validates exact split inputs before clearing, stops if the server
    // discovers a waiting pipeline, and uses the post-clear book revision on success.
    if (mobile) await $('[data-writer-pane="editor"]').click();
    await stage('chapter');
    await $('#writerSplitCount').fill('4');
    await $('#writerChapterNote').fill('指定拆分备注');
    const overwrite = async () => {
      await $('#writerSplitStart').click();
      await visible('#writerSplitDialog');
      confirmNext();
      await $('#writerSplitOverwriteChoice').click();
    };
    await control({ scenario: 'over_budget' });
    await overwrite();
    await page.waitForFunction(() => document.querySelector('#fixtureMessage').textContent.includes('模拟超窗'));
    let requests = (await status()).requests.filter(({ method }) => method === 'POST');
    const preflightBody = requests.find(({ path, body }) => path.endsWith('/preview') && body.kind === 'split').body;
    assert.equal(preflightBody.from_no, 1);
    assert.equal(preflightBody.want_count, 4);
    assert.equal(preflightBody.extra_note, '指定拆分备注');
    assert.ok(!requests.some(({ path }) => /\/(pipelines|clear)$/.test(path)));
    await control({ scenario: 'success' });
    await control({ step: 'state' }); // server only: UI has not observed it yet
    await overwrite();
    await visible(pending);
    await title('章末状态');
    requests = (await status()).requests;
    assert.ok(requests.some(({ path }) => path.endsWith('/pipeline')));
    assert.ok(!requests.some(({ path }) => /\/(pipelines|clear)$/.test(path)));
    confirmNext();
    await $('#writerPipelineCancel').click();
    await hidden(pending);
    const cancel = (await status()).requests.find(({ path }) => path.endsWith('/pipeline/cancel')).body;
    assert.equal(cancel.step, 'state');
    assert.ok(cancel.pipeline_id && cancel.decision_revision);
    if (mobile) await $('[data-writer-pane="editor"]').click();
    await overwrite();
    await page.waitForFunction(() => document.querySelector('#writerSaveState').textContent === '已提交拆分');
    requests = (await status()).requests.filter(({ path, body }) => (path.endsWith('/preview') && body.kind === 'split') || /\/(pipelines|clear)$/.test(path));
    assert.match(requests.at(-3).path, /\/preview$/);
    assert.match(requests.at(-2).path, /\/clear$/);
    assert.match(requests.at(-1).path, /\/pipelines$/);
    assert.ok(requests.at(-1).body.revision > requests.at(-2).body.revision);

    // Late successful decision from a different book must not overwrite the current book,
    // and correcting a nameless row keeps its other fields in the structured supplement.
    await seed('chars'); await title('角色设定'); await openNotes();
    await text('#writerPipelinePendingCounts', /已有 1 · 目标 2/);
    await visible('#writerPipelineDecisionAccept');
    await page.getByLabel('角色姓名', { exact: true }).nth(1).fill('小岚');
    assert.equal(await page.getByLabel('身份', { exact: true }).nth(1).inputValue(), '收信人');
    await control({ scenario: 'success', delay: 1 });
    const outgoing = waitRequest('/pipeline/decision');
    await $(supplement).click();
    const request = await outgoing;
    assert.equal(request.postDataJSON().content[1].name, '小岚');
    assert.equal(request.postDataJSON().content[1].identity, '收信人');
    await switchBook('#fixtureOtherBook', '第二本隔离作品');
    await title('题材推断');
    const response = await request.response();
    assert.equal(await response.ok(), true);
    await title('题材推断');
    await visible(pending);
    assert.equal(await $(history).count(), 0);

    await control({ scenario: 'success' });
    for (const [step, heading, field, initial] of [
      ['title', '书名候选', editor, '原书名'],
      ['blurb', '作品简介', 'textarea', '一段简介。\n另起一段。'],
      ['split', '拆章', 'input', '已识别章节'],
    ]) {
      await seed(step); await title(heading); await openNotes();
      const input = $(`#writerPipelinePendingFields ${field}`).first();
      assert.equal(await input.inputValue(), initial);
    }
    assert.deepEqual(errors, []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    console.log(`Writer intervention ${viewport.width}px: passed`);
  } finally {
    await page.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: process.env.NAIBA_SMOKE_BROWSER || 'msedge', headless: true });
  try {
    for (const viewport of viewports) await checkWriter(browser, viewport);
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
