// 真实产品页面 + 离线接口夹具：逐元素几何扫描手机长标题、稿件、版本和管理记录。
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const OUTPUT = path.resolve(process.env.NAIBA_SMOKE_OUTPUT || __dirname);
fs.mkdirSync(OUTPUT, { recursive: true });
const LONG = '尾声：当所有副本都关闭之后她终于回到了那间没有窗的出租屋'.repeat(5);
const BOOK = '《恐怖副本：大BOSS是恋爱脑》作者：姜乐你'.repeat(3);
const TOKEN = 'SourceMetadataWithoutSpaces'.repeat(30);
const NID = 'book-mobile-layout', WID = 'wb-mobile-layout';
const oldTime = 1750000000000;
const manuscript = (id, extra = {}) => ({id, kind:'original', kind_label:'原文', label:'原文', len:3000,
  created_at:oldTime, source_manuscript_id:'', chapter_no:157, ...extra});
const manuscripts = [manuscript('ms-original'), manuscript('ms-adapted', {
  kind:'ai',label:'改编稿 v4 · '+LONG,source_manuscript_id:'ms-original',broken:true,
}), manuscript('ms-good',{kind:'manual',label:'指定改编稿 · '+LONG,source_manuscript_id:'ms-original'})];
const chapters = [{i:157,title:'第157章 '+LONG,len:3000,manuscript_count:2,adapted_count:1,
  base_manuscript_id:'ms-original',base_kind:'original'}];
const novelBook = {id:NID,title:BOOK,revision:3,chapter_count:1,created_at:oldTime,updated_at:oldTime,
  source:{name:BOOK+'.txt',encoding:'utf-8',split_level:'heading',total_len:3000},
  elements:{characters:['姜乐：主角'],scenes:[],props:[],raw:'## 人物\n姜乐：主角'},
  elements_versions:[{version:1,raw:'## 人物\n姜乐：主角',updated_at:oldTime}],
  outline:LONG,outline_versions:[{version:12,outline:LONG,updated_at:oldTime}],script_version:1,
  script:[{i:1,scene:LONG,content:'深夜旧船。',from_chapter:157}],
  script_versions:[{version:1,script:[{i:1,scene:LONG,content:'深夜旧船。'}],updated_at:oldTime}]};
const writerBook = {id:WID,title:BOOK,revision:3,chapter_count:1,created_at:oldTime,updated_at:oldTime,
  current_setup_version_id:'su-one',current_outline_version_id:'ol-one',active_pipeline:null,
  setup_versions:[{id:'su-one',version:1,kind:'manual',title:BOOK,updated_at:oldTime,len:3000}],
  outline_versions:[{id:'ol-one',version:1,kind:'manual',title:LONG,updated_at:oldTime,len:3000,chapter_count:1}]};
const setup = {id:'su-one',version:1,title:BOOK,idea:LONG,worldview:LONG,blurb:LONG,
  genre:{channel:'male',cat:'悬疑',tone:'克制'},genre_tags:['悬疑'],characters:[{name:'姜乐',role:'主角'}],constraints:{}};
const writerChapter = {no:157,title:LONG,beat:LONG,revision:1,current_version_id:'cv-one',current_kind:'manual',len:3000,version_count:1};
const records = {book:novelBook,chapters,manuscripts,
  elements:[{kind:'elements',id:'el-one',version:1,len:3000,updated_at:oldTime,chapter_ids:[157]}],
  outlines:[{kind:'outline',id:'ol-one',version:12,title:LONG,len:3000,updated_at:oldTime,chapter_ids:[157]}],
  scripts:[{kind:'script',id:'sc-one',version:1,scene_count:1,updated_at:oldTime,chapter_ids:[157]}]};

async function geometry(page, selector, label) {
  const problems = await page.locator(selector).evaluate(root => {
    const limit = window.innerWidth, bad = [];
    const scrolling = '.novel-stages,.writer-stages,.novel-manage-tabs,.writer-manage-tabs';
    for (const el of [root,...root.querySelectorAll('*')]) {
      if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) continue;
      const allowed = el.closest(scrolling);
      if (allowed && allowed !== el) continue;
      if (rect.left < -1 || rect.right > limit + 1) bad.push({
        tag:el.tagName,id:el.id,cls:el.className,left:rect.left,right:rect.right,
        text:(el.textContent || '').slice(0,55),
      });
    }
    return bad.slice(0,12);
  });
  assert.deepEqual(problems, [], `${label} 几何越界：${JSON.stringify(problems)}`);
}

async function fixture(page) {
  const json = (route, body, status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  await page.addInitScript(({nid,wid}) => {
    localStorage.clear();localStorage.setItem('naibaOnboardingDismissed','1');
    localStorage.setItem('naibaNovelLastBookId',nid);localStorage.setItem('naibaWriterLastBookId',wid);
  }, {nid:NID,wid:WID});
  await page.route('**/api/providers/models', route=>json(route,{models:[]}));
  await page.route('**/api/studio/tasks**', route=>json(route,{tasks:[]}));
  await page.route('**/api/novel/**', route=>{
    const url = new URL(route.request().url()), p=url.pathname;
    if (p === '/api/novel/books') return json(route,{books:[novelBook],corrupt:[]});
    if (p === `/api/novel/books/${NID}`) return json(route,{book:novelBook,chapters,broken:[]});
    if (p === `/api/novel/books/${NID}/chapters/157`) return json(route,{
      ...chapters[0],content:'旧船的照片仍在。'.repeat(40),revision:1,manuscripts,
      base_manuscript_id:'ms-original',content_manuscript_id:'ms-original'});
    if (p.includes('/manuscripts/')) return json(route,{content:'原稿正文。'.repeat(20),manuscripts});
    if (p.endsWith('/records')) return json(route,records);
    if (p.endsWith('/script')) return json(route,{script:novelBook.script});
    if (p === '/api/novel/location') return json(route,{novel_root:'X:/fixture/'+TOKEN+'/exports/'+TOKEN+'.md'});
    if (p === '/api/novel/stage/preview') return json(route,{warnings:[],sources:{},chapters:[],budget:{fits:true,suggestions:[]}});
    return json(route,{});
  });
  await page.route('**/api/writer/**', route=>{
    const p=new URL(route.request().url()).pathname;
    if (p === '/api/writer/novel-import/preview') return json(route,{ready:true,book_id:NID,
      end_chapter_no:157,chapter_count:1,new_chapter_no:158,fingerprint:'fixture',
      chapter_sources:[{no:157,title:LONG,manuscripts}],
      coverage:{imported_chapters:1,characters:12,recent_chapters:1,recent_characters:9,relations:18,divergent_chapters:1}});
    if (p === '/api/writer/books') return json(route,{books:[writerBook],corrupt:[]});
    if (p === `/api/writer/books/${WID}`) return json(route,{book:writerBook,setup,
      outline:{id:'ol-one',version:1,chapters:[writerChapter],parts:[{no:1,title:LONG,text:LONG}]},chapters:[writerChapter],broken:[]});
    if (p === `/api/writer/books/${WID}/chapters/157`) return json(route,{...writerChapter,
      content:'旧船的照片仍在。'.repeat(40),versions:[{id:'cv-one',version:1,kind:'manual',len:3000,updated_at:oldTime}]});
    if (p.endsWith('/records')) return json(route,{book:writerBook,setup:writerBook.setup_versions,
      outlines:writerBook.outline_versions,chapters:[writerChapter],tasks:[]});
    if (p.endsWith('/pipeline')) return json(route,{pipeline:null});
    if (p === '/api/writer/location') return json(route,{writer_root:'X:/fixture/'+TOKEN+'/exports/'+TOKEN+'.md'});
    if (p === '/api/writer/generate/preview') return json(route,{budget:{fits:true,included:[],omitted:[],suggestions:[]}});
    return json(route,{});
  });
}

(async()=>{
  const browser = await chromium.launch({channel:'msedge',headless:true});
  let scans=0;
  try {
    const page = await browser.newPage({viewport:{width:393,height:852}});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await fixture(page);await page.goto(BASE,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.querySelector('#openStudio'));
    await page.evaluate(async()=>{
      const studio=await import('/js/22-studio.js');studio.openStudioView();studio.setStudioPage('novel');
    });
    await page.waitForSelector('#novelManuscripts .novel-manuscript-card');
    for (const width of [320,360,393,412]) {
      await page.setViewportSize({width,height:852});
      for (const theme of ['light','dark']) {
        await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
        await page.evaluate(async()=>{(await import('/js/22-studio.js')).setStudioPage('novel')});
        await page.waitForSelector('#novelManuscripts .novel-manuscript-card');
        for (const stage of ['split','elements','outline','script','storyboard']) {
          await page.locator(`[data-novel-stage="${stage}"]`).click();
          for (const pane of ['chapters','editor','preview']) {
            await page.locator(`[data-novel-pane="${pane}"]`).click();
            await geometry(page,'#studioPageNovel',`novel ${width}/${theme}/${stage}/${pane}`);scans++;
          }
        }
        await page.locator('[data-novel-stage="split"]').click();
        await page.locator('[data-novel-pane="editor"]').click();
        if (width===393 && theme==='light') await page.screenshot({path:path.join(OUTPUT,'novel-mobile-393.png')});
        await page.locator('#novelManageBtn').click();
        await page.locator('#novelManageTabs [data-novel-manage-tab="manuscript"]').click();
        await page.waitForSelector('#novelManageList .novel-manage-row');
        for (const tab of ['manuscript','elements','outline','script','task']) {
          await page.locator(`#novelManageTabs [data-novel-manage-tab="${tab}"]`).click();
          await page.locator('#novelManageError').evaluate((el,text)=>el.textContent=text,TOKEN);
          await geometry(page,'#novelManageDialog',`novel 管理记录 ${width}/${theme}/${tab}`);scans++;
        }
        await page.evaluate(()=>document.querySelector('#novelManageDialog').close());
        await page.evaluate(async()=>{(await import('/js/22-studio.js')).setStudioPage('writer')});
        await page.waitForFunction(()=>document.querySelector('#writerBookTitle')?.textContent.length>8);
        for (const stage of ['setup','outline','chapter','draft']) {
          await page.locator(`[data-writer-stage="${stage}"]`).click();
          for (const pane of ['book','editor','notes']) {
            await page.locator(`[data-writer-pane="${pane}"]`).click();
            await geometry(page,'#studioPageWriter',`writer ${width}/${theme}/${stage}/${pane}`);scans++;
          }
        }
        await page.locator('[data-writer-stage="draft"]').click();
        await page.locator('[data-writer-pane="book"]').click();
        await page.locator('[data-writer-chapter="157"]').click();
        await page.locator('[data-writer-pane="editor"]').click();
        await geometry(page,'#studioPageWriter',`writer 正文 ${width}/${theme}`);scans++;
        if (width===393 && theme==='light') await page.screenshot({path:path.join(OUTPUT,'writer-mobile-393.png')});
        await page.locator('#writerManageBtn').click();
        for (const tab of ['setup','outline','chapter','task']) {
          await page.locator(`#writerManageTabs [data-writer-manage-tab="${tab}"]`).click();
          if (tab==='chapter') {
            await page.locator('[data-writer-manage-open="chapter:157"]').click();
            await page.waitForSelector('[data-writer-manage-versions="157"]');
          }
          await page.locator('#writerManageError').evaluate((el,text)=>el.textContent=text,TOKEN);
          await geometry(page,'#writerManageDialog',`writer 管理记录 ${width}/${theme}/${tab}`);scans++;
        }
        await page.locator('#writerManageClose').click();
        await page.locator('#writerNovelContinueBtn').click();
        await page.waitForSelector('#writerNovelSourceBook option[value="'+NID+'"]',{state:'attached'});
        await geometry(page,'#writerNovelImportDialog',`续写向导 ${width}/${theme}/1`);scans++;
        await page.locator('#writerNovelSourceBook').selectOption(NID);
        await page.locator('#writerNovelImportNext').click();
        await page.waitForSelector('input[name="writerNovelSourceKind"]');
        await geometry(page,'#writerNovelImportDialog',`续写向导 ${width}/${theme}/2`);scans++;
        await page.locator('input[name="writerNovelSourceKind"][value="manuscript"]').check();
        await page.locator('#writerNovelImportNext').click();
        await page.waitForSelector('[data-novel-source-chapter="157"]');
        await page.locator('[data-novel-source-chapter="157"]').selectOption('ms-good');
        await geometry(page,'#writerNovelImportDialog',`续写向导 ${width}/${theme}/3`);scans++;
        await page.locator('#writerNovelImportNext').click();
        await page.waitForSelector('[data-continuation-summary]');
        for (const field of await page.locator('[data-continuation-summary]').all()) await field.fill(LONG);
        await geometry(page,'#writerNovelImportDialog',`续写向导 ${width}/${theme}/4`);scans++;
        await page.locator('#writerNovelImportNext').click();
        await page.waitForSelector('#writerNovelNewTitle');
        await geometry(page,'#writerNovelImportDialog',`续写向导 ${width}/${theme}/5`);scans++;
        if (width===393 && theme==='light') await page.screenshot({path:path.join(OUTPUT,'writer-import-mobile-393.png')});
        await page.locator('#writerNovelImportClose').click();
        console.log(`通过 ${width}px/${theme}：拆书匠五阶段、织书台四阶段、三分栏、稿件与管理记录几何`);
      }
    }
    assert.deepEqual(errors,[],'页面脚本错误');
    fs.writeFileSync(path.join(OUTPUT,'mobile-geometry-report.json'),JSON.stringify({
      base:BASE,scans,widths:[320,360,393,412],themes:['light','dark'],
      criterion:'getBoundingClientRect',allowedScroll:'阶段条和管理分类条的后代',passed:true,
    },null,2)+'\n');
    console.log(`手机几何验收通过：${scans} 个页面状态，判据为 getBoundingClientRect`);
  } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
