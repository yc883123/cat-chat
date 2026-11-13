// 固定本机源码与隔离数据；模型边界替身，TXT执行、存储和消费走生产链路。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const fixture = JSON.parse(fs.readFileSync(process.env.NAIBA_WORKBENCH_TXT_FIXTURE, 'utf8'));
const BASE = fixture.base, OUTPUT = fixture.runtime;

async function json(context, endpoint, method = 'get', body) {
  const response = await context.request[method](BASE + endpoint, body === undefined ? {} : { data: body });
  assert.ok(response.ok(), `${method} ${endpoint}: ${response.status()} ${await response.text()}`);
  return response.json();
}
async function taskDone(context, id, status = 'done') {
  for (let attempt = 0; attempt < 160; attempt++) {
    const { task } = await json(context, `/api/studio/tasks/${id}`);
    if (['done', 'failed', 'cancelled', 'interrupted'].includes(task.status)) {
      assert.equal(task.status, status, JSON.stringify(task)); return task;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw Error(`任务未结束：${id}`);
}
async function enter(page, domain, book) {
  await page.evaluate(async ({ domain, bookId }) => {
    const studio = await import('/js/22-studio.js'); studio.openStudioView(); studio.setStudioPage(domain);
    window.dispatchEvent(new CustomEvent('naiba-open-domain', { detail: { domain, bookId } }));
  }, { domain, bookId: book.id });
  await page.waitForFunction(({ domain, title }) => document.querySelector(`#${domain}BookTitle`)?.textContent.includes(title),
    { domain, title: book.title });
}
async function pane(page, domain, name) {
  const button = page.locator(`#${domain}Panes [data-${domain}-pane="${name}"]`);
  if (await button.isVisible()) await button.click();
}
async function run(browser, source) {
  const width = source.width;
  const context = await browser.newContext({ viewport: { width, height: width === 375 ? 812 : 900 }, acceptDownloads: true });
  await context.addInitScript(() => {
    localStorage.setItem('naibaOnboardingDismissed', '1');
    localStorage.setItem('naibaChatToken', 'txt-download-fixture');
  });
  const page = await context.newPage(), errors = [];
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('response', response => { if (response.status() >= 400 && !new URL(response.url()).pathname.endsWith('/favicon.ico')) errors.push(`${response.status()} ${response.url()}`); });
  page.on('dialog', dialog => dialog.accept());
  const checkNoScriptError = async () => {
    assert.deepEqual(errors, []);
    assert.equal(await page.locator('body').evaluate(node => /JS ERROR|PROMISE REJECT/.test(node.innerText)), false);
  };
  const artifactBar = anchor => page.locator(anchor).locator('xpath=preceding-sibling::*[1]');
  const assertDownload = async (bar, task, expected, label) => {
    const waiting = page.waitForEvent('download');
    const requesting = page.waitForRequest(request => /\/artifact\?download=1$/.test(request.url()));
    await bar.getByRole('button', { name: /下载.*TXT/ }).click();
    assert.equal((await requesting).headers().authorization, 'Bearer txt-download-fixture', 'TXT fetch显式携带会话Bearer');
    const download = await waiting;
    const destination = path.join(OUTPUT, `${label}-${width}.txt`);
    await download.saveAs(destination);
    const bytes = fs.readFileSync(destination);
    assert.equal(bytes.toString('utf8'), expected);
    if (task.result_artifact.status === 'final') {
      assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), task.result_artifact.sha256);
    }
    await checkNoScriptError();
  };
  const writerResume = async () => {
    const book = source.writer_resume;
    const started = (await json(context, `/api/writer/books/${book.id}/pipelines`, 'post', {
      kind: 'setup_step', step: 'world', model_key: 'online:txt-fixture', model_name: 'txt-fixture',
      extra_note: `TXT_WPARTIAL_${width}`,
    })).pipeline;
    await enter(page, 'writer', book);
    const partial = await taskDone(context, started.current_task_id, 'failed');
    assert.equal(partial.result_artifact.status, 'partial');
    let stopped;
    for (let attempt = 0; attempt < 160; attempt++) {
      stopped = (await json(context, `/api/writer/books/${book.id}/pipeline`)).pipeline;
      if (stopped.status === 'failed') break;
      await page.waitForTimeout(100);
    }
    assert.equal(stopped?.status, 'failed', JSON.stringify(stopped));
    // 刷新后仍须能从持久current_task_id补读TXT状态，不能依赖终态过渡窗口。
    await page.reload({ waitUntil: 'networkidle' });
    await enter(page, 'writer', book);
    await pane(page, 'writer', 'notes');
    const bar = artifactBar('#writerPipelineStream');
    await bar.getByRole('button', { name: '继续未完成内容' }).waitFor({ state: 'visible' });
    await bar.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(OUTPUT, `writer-partial-${width}.png`) });
    await checkNoScriptError();
    const before = await json(context, `/api/writer/books/${book.id}`);
    assert.equal(before.book.setup_versions.length, 1, 'partial尚未创建世界观版本');
    const waiting = page.waitForResponse(response => new URL(response.url()).pathname === `/api/writer/books/${book.id}/pipeline/resume`);
    await bar.getByRole('button', { name: '继续未完成内容' }).click();
    const response = await waiting;
    assert.ok(response.ok(), await response.text());
    const resumed = (await response.json()).pipeline;
    assert.equal(resumed.attempt, started.attempt);
    const childId = resumed.current_task_id || resumed.result_resume?.child_task_id;
    assert.ok(childId && childId !== partial.id, JSON.stringify(resumed));
    const child = await taskDone(context, childId);
    assert.equal(child.logical_generation_id, partial.logical_generation_id);
    assert.equal(child.input.writer.attempt, partial.input.writer.attempt);
    assert.equal(child.result, fixture.world, 'Writer接续只补未完成尾文');
    let pipeline;
    for (let attempt = 0; attempt < 160; attempt++) {
      pipeline = (await json(context, `/api/writer/books/${book.id}/pipeline`)).pipeline;
      if (['done', 'failed', 'conflict', 'cancelled', 'interrupted'].includes(pipeline.status)) break;
      await page.waitForTimeout(100);
    }
    assert.equal(pipeline.status, 'done', JSON.stringify(pipeline));
    assert.equal(pipeline.attempt, started.attempt);
    let saved = await json(context, `/api/writer/books/${book.id}`);
    assert.equal(saved.setup.worldview, fixture.world.trim());
    assert.equal(saved.book.setup_versions.length, 2);
    assert.equal(saved.book.setup_versions.at(-1).task_id, partial.logical_generation_id);
    // 再发同一恢复命令仅返回已提交结果，不能新建任务/版本或再次请求模型。
    const command = response.request().postDataJSON();
    const duplicate = (await json(context, `/api/writer/books/${book.id}/pipeline/resume`, 'post', command)).pipeline;
    assert.equal(duplicate.status, 'done');
    saved = await json(context, `/api/writer/books/${book.id}`);
    assert.equal(saved.book.setup_versions.length, 2);
    await checkNoScriptError();
    console.log(`Writer TXT恢复 ${width}px：界面继续→真实调度→同attempt/logicalID写回一次通过`);
  };
  const generateOutline = async (marker, expectedStatus = 'done') => {
    await page.locator('#novelStageNote').fill(marker);
    await page.waitForFunction(() => !document.querySelector('#novelStageGenerate').disabled);
    const waiting = page.waitForResponse(response => new URL(response.url()).pathname === '/api/novel/stage' && response.request().method() === 'POST');
    await page.locator('#novelStageGenerate').click();
    const response = await waiting;
    assert.ok(response.ok(), await response.text());
    const { task } = await response.json();
    const finished = await taskDone(context, task.id, expectedStatus);
    await checkNoScriptError();
    return finished;
  };
  try {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    if (process.env.NAIBA_TXT_ONLY_WRITER_RESUME) {
      await writerResume();
      return;
    }
    if (!process.env.NAIBA_TXT_ONLY_WRITER) {
    await enter(page, 'novel', source.novel);
    await page.locator('#novelStages [data-novel-stage="outline"]').click();
    await pane(page, 'novel', 'chapters');
    await page.locator('[data-novel-pick="1"]').check();
    await pane(page, 'novel', 'editor');
    const outline = await generateOutline(`TXT_OUTLINE_${width}`);
    assert.equal(outline.result_artifact.status, 'final');
    await page.waitForFunction(() => document.querySelector('#novelStageTask .wb-result-artifact')?.textContent.includes('TXT 已完成'));
    const outlineBar = artifactBar('#novelStageResult');
    assert.equal(await outlineBar.evaluate(node => {
      const rect = node.getBoundingClientRect(); return rect.left >= -1 && rect.right <= innerWidth + 1;
    }), true, 'TXT控件不能横向溢出视口');
    await outlineBar.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(OUTPUT, `workbench-outline-${width}.png`) });
    await outlineBar.getByRole('button', { name: '查看 TXT' }).click();
    await page.locator('.wb-result-preview[open]').waitFor();
    assert.equal(await page.locator('.wb-result-preview pre').textContent(), fixture.outline);
    await page.locator('.wb-result-preview').getByRole('button', { name: '关闭' }).click();
    await checkNoScriptError();
    await assertDownload(outlineBar, outline, fixture.outline, 'outline');
    await page.locator('#novelStageApply').click();
    await page.waitForFunction(() => document.querySelector('#novelStageSavedBody')?.textContent.includes('旧船调查'));
    const saved = await json(context, `/api/novel/books/${source.novel.id}`);
    assert.equal(saved.book.outline, fixture.outline);
    await page.reload({ waitUntil: 'networkidle' }); await enter(page, 'novel', source.novel);
    await page.locator('#novelStages [data-novel-stage="outline"]').click(); await pane(page, 'novel', 'editor');
    assert.ok((await page.locator('#novelStageSavedBody').textContent()).includes('旧船调查'));
    await checkNoScriptError();

    // 第一段已确认后网络中断。partial不可写回；显式继续保留logicalID、只补第二段。
    await pane(page, 'novel', 'chapters'); await page.locator('[data-novel-pick="1"]').check();
    await pane(page, 'novel', 'editor');
    const partial = await generateOutline(`TXT_PARTIAL_${width}`, 'failed');
    assert.equal(partial.result_artifact.status, 'partial');
    await page.waitForFunction(() => document.querySelector('#novelStageTask .wb-result-artifact')?.textContent.includes('TXT 未完成'));
    await artifactBar('#novelStageResult').getByRole('button', { name: '继续未完成内容' }).waitFor({ state: 'visible' });
    await artifactBar('#novelStageResult').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(OUTPUT, `workbench-partial-${width}.png`) });
    assert.equal(await page.locator('#novelStageApply').isVisible(), false);
    const rejected = await context.request.post(`${BASE}/api/novel/books/${source.novel.id}/apply`, {
      data: { stage: 'outline', task_id: partial.id, text: '不得保存此伪造结果', revision: saved.book.revision },
    });
    assert.equal(rejected.status(), 409);
    const oldArtifact = await json(context, `/api/studio/tasks/${partial.id}/artifact`);
    assert.ok(fixture.outline.startsWith(oldArtifact.text));
    const resuming = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/resume-result'));
    await artifactBar('#novelStageResult').getByRole('button', { name: '继续未完成内容' }).click();
    const { task: child } = await (await resuming).json();
    assert.notEqual(child.id, partial.id);
    assert.equal(child.logical_generation_id, partial.logical_generation_id);
    const resumed = await taskDone(context, child.id);
    assert.equal(resumed.result, fixture.outline, '恢复只拼接缺少的第二段，不重复前文');
    await page.waitForFunction(() => document.querySelector('#novelStageTask .wb-result-artifact')?.textContent.includes('TXT 已完成'));
    await assertDownload(artifactBar('#novelStageResult'), resumed, fixture.outline, 'resumed');
    await checkNoScriptError();

    // 完成文件损坏后旧result不能成为保存后门。
    await json(context, '/_fixture/corrupt', 'post', { task_id: resumed.id });
    const bad = await json(context, `/api/studio/tasks/${resumed.id}`);
    assert.equal(bad.task.result_artifact.status, 'invalid');
    const badSave = await context.request.post(`${BASE}/api/novel/books/${source.novel.id}/apply`, {
      data: { stage: 'outline', task_id: resumed.id, text: fixture.outline, revision: saved.book.revision },
    });
    assert.equal(badSave.status(), 409);
    await checkNoScriptError();

    // 单章AI改编使用manuscripts保存入口，文件原文为服务端权威来源。
    await page.locator('#novelStages [data-novel-stage="split"]').click();
    await pane(page, 'novel', 'chapters');
    await page.locator('[data-novel-chapter="1"]').click(); await pane(page, 'novel', 'editor');
    await page.locator('[data-novel-manuscript-action="ai"]').first().click();
    await page.locator('#novelAiRequest').fill(`TXT_ADAPT_${width}`);
    const adapting = page.waitForResponse(response => new URL(response.url()).pathname === '/api/novel/stage' && response.request().method() === 'POST');
    await page.locator('#novelAiSubmit').click();
    const { task: adaptTask } = await (await adapting).json();
    const adapted = await taskDone(context, adaptTask.id);
    await page.waitForFunction(() => document.querySelector('#novelAiSubmit')?.textContent.includes('保存改编稿'));
    await assertDownload(artifactBar('#novelAiResult'), adapted, fixture.adapt, 'adapt');
    const savingAdapt = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/manuscripts') && response.request().method() === 'POST');
    await page.locator('#novelAiSubmit').click();
    const adaptSave = await savingAdapt;
    assert.ok(adaptSave.ok(), await adaptSave.text());
    const manuscript = (await adaptSave.json()).manuscript;
    assert.equal(manuscript.task_id, adapted.logical_generation_id);
    await checkNoScriptError();

    }
    // Writer单步世界观经过真实流水线自动写回；生成记录提供TXT操作。
    await json(context, `/api/writer/books/${source.writer.id}/pipelines`, 'post', {
      kind: 'setup_step', step: 'world', model_key: 'online:txt-fixture', model_name: 'txt-fixture', extra_note: `TXT_WORLD_${width}`,
    });
    await enter(page, 'writer', source.writer);
    let worldPipeline;
    for (let attempt = 0; attempt < 160; attempt++) {
      worldPipeline = (await json(context, `/api/writer/books/${source.writer.id}/pipeline`)).pipeline;
      if (['done', 'failed', 'interrupted', 'cancelled', 'conflict'].includes(worldPipeline?.status)) break;
      await page.waitForTimeout(100);
    }
    assert.equal(worldPipeline?.status, 'done', JSON.stringify(worldPipeline));
    const writerBook = await json(context, `/api/writer/books/${source.writer.id}`);
    fs.writeFileSync(path.join(OUTPUT, `writer-world-${width}.json`), JSON.stringify(writerBook, null, 2));
    assert.equal(writerBook.setup.worldview, fixture.world.trim());
    const list = await json(context, '/api/studio/tasks?module=writer');
    const worldTask = list.tasks.find(task => task.writer?.book_id === source.writer.id || task.input?.writer?.book_id === source.writer.id);
    assert.ok(worldTask);
    const world = await taskDone(context, worldTask.id);
    await page.locator('#writerMoreBtn').click(); await page.locator('#writerManageBtn').click();
    await page.locator('[data-writer-manage-tab="task"]').click();
    await page.locator(`[data-writer-manage-open="task:${world.id}"]`).click();
    await assertDownload(artifactBar('#writerManagePreviewBody'), world, fixture.world, 'world');
    await page.locator('#writerManageClose').click(); await checkNoScriptError();

    // 浏览器手改保存；再检查服务端对CRLF/emoji的无损保存及刷新重放。
    await page.locator('#writerStages [data-writer-stage="draft"]').click();
    await pane(page, 'writer', 'book');
    await page.locator('[data-writer-chapter="1"]').click(); await pane(page, 'writer', 'editor');
    await page.waitForFunction(() => document.querySelector('#writerDraftTitle')?.textContent.includes('来信'));
    const manual = `手工正文${width}：港口的月亮🌙\n\n新的一行保留空白。`;
    await page.locator('#writerDraftContent').fill(manual);
    const savingManual = page.waitForResponse(response => /\/api\/writer\/books\/.*\/chapters\/1$/.test(new URL(response.url()).pathname) && response.request().method() === 'PATCH');
    await page.locator('#writerDraftSave').click(); assert.ok((await savingManual).ok());
    let chapter = await json(context, `/api/writer/books/${source.writer.id}/chapters/1`);
    assert.equal(chapter.content, manual);
    const crlf = `  CRLF${width}🌙\r\n\r\n尾行保留空格  \r\n`;
    await json(context, `/api/writer/books/${source.writer.id}/chapters/1`, 'patch', { content: crlf, revision: chapter.revision });
    chapter = await json(context, `/api/writer/books/${source.writer.id}/chapters/1`);
    assert.equal(chapter.content, crlf);
    await page.reload({ waitUntil: 'networkidle' }); await enter(page, 'writer', source.writer);
    await page.locator('#writerStages [data-writer-stage="draft"]').click();
    await pane(page, 'writer', 'book'); await page.locator('[data-writer-chapter="1"]').click(); await pane(page, 'writer', 'editor');
    await page.waitForFunction(expected => document.querySelector('#writerDraftContent')?.value === expected, crlf.replaceAll('\r\n', '\n'));
    assert.equal(await page.locator('#writerDraftContent').inputValue(), crlf.replaceAll('\r\n', '\n'), 'textarea按HTML规范显示换行，服务端原始CRLF保留');
    await checkNoScriptError();
    await page.screenshot({ path: path.join(OUTPUT, `workbench-txt-${width}.png`) });
    await writerResume();
    console.log(`TXT ${width}px：outline/adapt/world、下载hash、partial恢复、invalid拒保存、人工正文与CRLF通过`);
  } catch (error) {
    await page.screenshot({ path: path.join(OUTPUT, `workbench-txt-failure-${width}.png`) }).catch(() => {});
    fs.writeFileSync(path.join(OUTPUT, `failure-${width}.txt`), (await page.locator('body').innerText().catch(() => '')) + '\n\n' + errors.join('\n'));
    throw error;
  } finally { await context.close(); }
}
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try { for (const source of fixture.sources) await run(browser, source); }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
