// 真页面 + 隔离服务：验证改版后的书名菜单、分栏、固定动作栏与原有流程入口。
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const fixturePath = process.env.NAIBA_WB_FIXTURE;
if (!fixturePath) throw Error('请设置 NAIBA_WB_FIXTURE，指向隔离服务的 fixture.json');
const fixture = JSON.parse(fs.readFileSync(fixturePath,'utf8'));
const BASE = process.env.NAIBA_SMOKE_BASE || fixture.base;
const output = process.env.NAIBA_SMOKE_OUTPUT || path.join(__dirname,'workbench-books-smoke');
fs.mkdirSync(output,{recursive:true});

(async()=>{
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const results=[];
  try {
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(f=>{
      localStorage.setItem('naibaOnboardingDismissed','1');
      localStorage.setItem('naibaNovelLastBookId',f.novel_book_id);
      localStorage.setItem('naibaWriterLastBookId',f.writer_book_id);
    },fixture);
    await page.goto(BASE,{waitUntil:'domcontentloaded'});
    await page.waitForSelector('#openStudio');
    for (const width of [1440,1280,1160,1024,900,390]) {
      await page.setViewportSize({width,height:900});
      for (const prefix of ['novel','writer']) {
        const root = '#studioPage'+(prefix==='novel'?'Novel':'Writer');
        await page.evaluate(async(prefix)=>{
          const studio=await import('/js/22-studio.js');studio.openStudioView();studio.setStudioPage(prefix);
        },prefix);
        await page.waitForFunction(({prefix,title})=>document.getElementById(prefix+'PageState').textContent.includes(title),
          {prefix,title:prefix==='novel'?fixture.novel_title:fixture.writer_title});
        await page.waitForFunction(root=>{
          const el=document.querySelector(root),width=el.getBoundingClientRect().width;
          return el.dataset.wbSize===(width<=760?'narrow':width<1000?'medium':'wide');
        },root);
        const size=await page.locator(root).getAttribute('data-wb-size');
        const stages=prefix==='novel'?['split','elements','outline','script','storyboard']:['setup','outline','chapter','draft'];
        for (const stage of stages) {
          await page.locator(`[data-${prefix}-stage="${stage}"]`).click();
          if (size==='narrow') await page.locator(`button[data-${prefix}-pane="editor"]`).click();
          if(prefix==='writer'&&stage==='draft') {
            if(size==='narrow') await page.locator('button[data-writer-pane="book"]').click();
            await page.locator('[data-writer-chapter]').first().click();
            if(size==='narrow') await page.locator('button[data-writer-pane="editor"]').click();
            await page.waitForFunction(()=>!document.querySelector('#writerDraftTitle')?.textContent.includes('还没有选择'));
            if(!(await page.locator('#writerDraftContent').inputValue())) {
              await page.locator('#writerDraftContent').fill('离线验收正文：雨停以后，渡口的灯还亮着。她展开旧信，终于看清了被水渍遮住的名字。\n\n此文本只留在隔离浏览器草稿中，不保存到作品，也不调用模型。');
            }
          }
          const primary = prefix==='novel'?(stage==='split'?null:'#novelStageGenerate'):
            ({setup:'#writerSetupGenerate',outline:'#writerOutlineGenerate',chapter:'#writerSplitStart',draft:'#writerDraftGenerate'})[stage];
          if (primary) {
            const rect=await page.locator(primary).boundingBox();
            assert.ok(rect&&rect.y>=0&&rect.y+rect.height<=900,`${prefix}/${stage}/${width} 主操作不在视口`);
            const panel=prefix==='novel'?'#novelStagePanel':`[data-writer-stage-panel="${stage}"]`;
            await page.locator(panel+' .wb-book-stage-body').evaluate(el=>{el.scrollTop=el.scrollHeight});
            const after=await page.locator(primary).boundingBox();
            assert.ok(Math.abs(after.y-rect.y)<=1,`${prefix}/${stage}/${width} 主操作没有固定`);
          }
          assert.deepEqual(errors,[],`${prefix}/${stage}/${width} 脚本异常`);
        }
        await page.locator('#'+prefix+'BookMenuBtn').click();
        assert.equal(await page.locator('#'+prefix+'BookMenu').isVisible(),true);
        assert.ok((await page.locator('#'+prefix+'BookMenuBtn').boundingBox()).width>=40);
        await page.locator('#'+prefix+'BooksBtn').click();
        await page.waitForSelector('#'+prefix+'BooksDialog[open]');
        await page.locator('#'+prefix+'BooksClose').click();
        await page.waitForFunction(prefix=>document.activeElement.id===prefix+'BookMenuBtn',prefix);
        // 键盘进入作品列表并用 Escape 关闭，也必须返回可见的书名入口。
        await page.keyboard.press('Enter');
        await page.waitForSelector('#'+prefix+'BookMenu');
        await page.keyboard.press('Enter');
        await page.waitForSelector('#'+prefix+'BooksDialog[open]');
        await page.keyboard.press('Escape');
        await page.waitForFunction(prefix=>document.activeElement.id===prefix+'BookMenuBtn',prefix);
        await page.locator('#'+prefix+'MoreBtn').click();
        const anchorRect=await page.locator('#'+prefix+'MoreBtn').boundingBox();
        await page.locator('#'+prefix+'ExportBtn').click();
        await page.waitForSelector('#'+prefix+'ExportMenu');
        assert.equal(await page.locator('#'+prefix+'ExportMenu').isVisible(),true);
        const exportRect=await page.locator('#'+prefix+'ExportMenu').boundingBox();
        assert.ok(exportRect.y>=anchorRect.y+anchorRect.height, '导出菜单必须锚在页头更多按钮下方，不能因父菜单关闭跳到左上角');
        assert.ok(exportRect.width<=320&&exportRect.x>=12&&exportRect.x+exportRect.width<=width-12,'导出菜单保持紧凑且不越界');
        const beforeEscape=await page.locator(root).evaluate(el=>({size:el.dataset.wbSize,width:el.getBoundingClientRect().width}));
        await page.keyboard.press('Escape');
        await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
        const afterEscape=await page.locator(root).evaluate(el=>({size:el.dataset.wbSize,width:el.getBoundingClientRect().width}));
        assert.deepEqual(afterEscape,beforeEscape,'菜单 Escape 不得影响侧栏和页面分档');
        assert.equal(await page.evaluate(()=>document.activeElement.id),prefix+'MoreBtn','导出关闭后回到仍可见的更多按钮');
        const currentSize=await page.locator(root).getAttribute('data-wb-size');
        console.log(JSON.stringify({width,prefix,size,currentSize,actualWidth:await page.locator(root).evaluate(el=>el.getBoundingClientRect().width)}));
        assert.equal(currentSize,size,'操作菜单不得改变当前分档');
        if(size==='medium') {
          await page.locator('#'+prefix+'AsideBtn').click();
          await page.waitForSelector('#'+prefix+'AsideClose');
          await page.locator('#'+prefix+'AsideClose').click();
          assert.equal(await page.locator('#'+prefix+'AsideBtn').getAttribute('aria-expanded'),'false');
        }
        if(prefix==='writer') {
          await page.locator('#writerBookMenuBtn').click();
          await page.locator('#writerNovelContinueBtn').click();
          await page.waitForSelector('#writerNovelImportDialog[open]');
          await page.locator('#writerNovelImportClose').click();
          await page.waitForFunction(()=>document.activeElement.id==='writerBookMenuBtn');
        }
        await page.screenshot({path:path.join(output,`${prefix}-${width}.png`)});
        assert.deepEqual(errors,[]);
        results.push({width,prefix,size,passed:true});
      }
    }
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({results,errors},null,2)+'\n');
    console.log(JSON.stringify({passed:true,views:results.length,results}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});
