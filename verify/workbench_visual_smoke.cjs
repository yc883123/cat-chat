// 隔离源码服务的视觉/导航守门；不提交生成，不访问外网。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8843';
const OUT = path.join(__dirname, 'workbench-visual-results');
const widths = (process.env.WB_WIDTHS || '1440,1024,900,390,375,320').split(',').map(Number);
const pages = ['library', 'labeler', 'comfyui', 'novel', 'writer', 'super'];
const results = [];

async function metrics(page, selector) {
  return page.evaluate(selector => {
    const root = document.querySelector(selector);
    const bounds = root.getBoundingClientRect();
    const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'
      && !el.closest('[inert]');
    const overflow = [], small = [], touch = [], brokenHidden = [], contrast = [];
    const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
    const context=canvas.getContext('2d');
    const rgba=color=>{context.clearRect(0,0,1,1);context.fillStyle=color;context.fillRect(0,0,1,1);return [...context.getImageData(0,0,1,1).data].map((v,i)=>i===3?v/255:v);};
    const over=(fg,bg)=>fg.slice(0,3).map((v,i)=>v*fg[3]+bg[i]*(1-fg[3]));
    const luminance=color=>color.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
    const ratio=el=>{
      const chain=[];let parent=el;
      while(parent){chain.unshift(parent);parent=parent.parentElement;}
      if(chain.some(node=>{const s=getComputedStyle(node);return parseFloat(s.opacity)<.99||s.backgroundImage!=='none';}))return null;
      let bg=[255,255,255];for(const node of chain)bg=over(rgba(getComputedStyle(node).backgroundColor),bg);
      const fg=over(rgba(getComputedStyle(el).color),bg),a=luminance(fg),b=luminance(bg);
      return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
    };
    for (const el of root.querySelectorAll('*')) {
      if (!visible(el)) continue;
      const rect = el.getBoundingClientRect(), css = getComputedStyle(el);
      if (rect.width <= 0 || rect.height <= 0) continue;
      const name = el.id || `${el.tagName.toLowerCase()}.${String(el.className).split(' ').join('.')}`;
      let clipped = false;
      for (let parent=el.parentElement; parent && parent !== root; parent=parent.parentElement) {
        if (['auto','scroll','hidden','clip'].includes(getComputedStyle(parent).overflowX)
            && parent.getBoundingClientRect().right <= bounds.right + 1) { clipped = true; break; }
      }
      if (!clipped && (rect.left < bounds.left - 1 || rect.right > bounds.right + 1))
        overflow.push({name,left:Math.round(rect.left),right:Math.round(rect.right)});
      if ([...el.childNodes].some(n=>n.nodeType===3 && n.textContent.trim())) {
        if(parseFloat(css.fontSize)<12)small.push({name,size:css.fontSize});
        if(!el.closest(':disabled,option')){const value=ratio(el);if(value!==null&&value<4.5)contrast.push({name,ratio:Number(value.toFixed(2)),color:css.color});}
      }
      if (innerWidth<=760 && el.matches('button,summary,input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select')
          && (rect.height<39.5 || rect.width<39.5)) touch.push({name,width:Math.round(rect.width),height:Math.round(rect.height)});
      if (el.hidden && !el.matches('.labeler-page-body')) brokenHidden.push(name);
    }
    return {width:Math.round(bounds.width),size:root.dataset.wbSize,overflow,small,touch,brokenHidden,contrast};
  }, selector);
}

async function main() {
  fs.mkdirSync(OUT, {recursive:true});
  const browser = await chromium.launch({channel:'msedge',headless:true});
  try {
    for (const width of widths) {
      const page = await browser.newPage({viewport:{width,height:900}});
      const errors=[];
      page.on('pageerror',e=>errors.push(e.message));
      page.on('console',m=>{if(m.type()==='error'&&!m.text().includes('Failed to load resource'))errors.push(m.text());});
      await page.route('**/*',route=>{
        const url=new URL(route.request().url());
        return ['127.0.0.1','localhost'].includes(url.hostname)||url.protocol==='data:' ? route.continue() : route.abort();
      });
      await page.addInitScript(()=>localStorage.setItem('naibaOnboardingDismissed','1'));
      await page.goto(BASE,{waitUntil:'load'});
      await page.waitForTimeout(700);
      for (const theme of ['light','dark']) {
        await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
        for (const name of pages) {
          await page.evaluate(async name=>{
            if(name==='super') {const m=await import('/js/28-super-agent.js');await m.openSuperAgentView();}
            else {const m=await import('/js/22-studio.js');m.openStudioView();m.setStudioPage(name,{persist:false});}
          },name);
          await page.waitForTimeout(300);
          assert.deepEqual(errors,[],`${name} ${width} ${theme} 不应有脚本错误`);
          if(name==='library') {
            if(theme==='light') await page.locator('#studioUserRequest').fill('验收草稿：切页与换主题后应保留。');
            else assert.equal(await page.locator('#studioUserRequest').inputValue(),'验收草稿：切页与换主题后应保留。');
          }
          const selector=name==='super'?'#superAgentView':`#studioPage${name[0].toUpperCase()+name.slice(1)}`;
          const snapshot=await metrics(page,selector);
          results.push({name,viewport:width,theme,...snapshot});
          await page.screenshot({path:path.join(OUT,`${name}-${width}-${theme}.png`)});
          if(name!=='super') {
            const nav=await page.evaluate(()=>{
              const button=document.getElementById(innerWidth<=760?'studioOpenSidebar':'studioExpandSidebar');
              return {count:document.querySelectorAll(`#${button.id}`).length,inside:!button.closest('[hidden]')};
            });
            assert.equal(nav.count,1);assert.equal(nav.inside,true,`${name} 导航不能留在隐藏页头`);
          }
          console.log(`${name} ${width} ${theme}: ${JSON.stringify(snapshot)}`);
        }
      }
      // 手机实际导航一进一出，再回到聊天检查输入区。
      if(width<=760){
        await page.evaluate(async()=>{const m=await import('/js/22-studio.js');m.openStudioView();m.setStudioPage('library',{persist:false});});
        await page.locator('#studioOpenSidebar').click();
      }
      await page.locator('#openChatMode').click();
      await page.waitForTimeout(150);
      const composer=await page.locator('#messageInput').boundingBox();
      assert.ok(composer&&composer.y+composer.height<=901,'聊天输入框必须留在视口');
      assert.deepEqual(errors,[]);
      await page.close();
    }
    fs.writeFileSync(path.join(OUT,'metrics.json'),JSON.stringify(results,null,2));
    const defects=results.filter(r=>r.overflow.length||r.brokenHidden.length||r.small.length||r.touch.length||r.contrast.length);
    assert.deepEqual(defects,[],'页面不能越界、违反hidden、字号小于12px、手机触控区小于40px或可测文字对比度低于4.5');
    console.log(`通过 ${results.length} 个页面/主题/宽度组合。`);
  } finally {
    fs.writeFileSync(path.join(OUT,'metrics.json'),JSON.stringify(results,null,2));
    await browser.close();
  }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
