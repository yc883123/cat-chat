// 任务总管：真实浏览器渲染，隔离只读任务快照；不提交模型或任务动作。
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8843';
const out = process.env.NAIBA_SMOKE_OUT || path.join(__dirname, 'workbench-super-artifacts');
(async () => {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  try {
    fs.mkdirSync(out,{recursive:true});
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    const errors=[];page.on('pageerror',error=>errors.push(String(error)));
    await page.addInitScript(()=>localStorage.setItem('naibaOnboardingDismissed','1'));
    let mode='ready', calls=[], otherTitle='另一个活任务';
    const task=()=>({thread_id:'wb-smoke',title:'任务总管刷新稳定性',goal:'这段目标文字在后台刷新时应保留选区',status:mode==='recovery'?'needs_recovery':mode==='completed'?'completed':'active',current_node_id:'wb-node',linked_conversation_ids:[],updated_at:1000});
    await page.route('**/api/super-agent/threads**',async route=>{
      const url = new URL(route.request().url());calls.push(url.pathname);
      if (route.request().method()!=='GET') throw new Error('冒烟不允许写任务');
      const node={node_id:'wb-node',kind:'chat',executor_type:'chat_run',status:mode==='recovery'?'interrupted':mode==='completed'?'succeeded':'ready',capabilities:{start:mode==='ready',retry:mode==='recovery'}};
      const data=url.pathname.endsWith('/nodes')?{nodes:[node]}:url.pathname.endsWith('/events')?{events:[],last_sequence:0}:url.pathname.endsWith('/artifacts')?{artifacts:[]}:url.pathname.endsWith('/approvals')?{approvals:[]}:url.pathname.endsWith('/threads')?{threads:[task(),...(mode==='completed'?[{thread_id:'other',status:'active',title:otherTitle}]:[])]}:{thread:task()};
      await route.fulfill({json:data});
    });
    await page.goto(base);
    await page.locator('#openSuperAgent').click();
    await page.locator('#superAgentFocusActions [data-super-action="start"]').waitFor();
    assert.match(await page.locator('#superAgentFocusTitle').textContent(),/尚未开始/);
    await page.screenshot({path:path.join(out,'tasks-desktop-ready.png')});
    await page.evaluate(()=>{
      window.savedAction=document.querySelector('#superAgentFocusActions button');window.savedAction.focus();
      const range=document.createRange();range.selectNodeContents(document.querySelector('#superAgentGoal'));
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);window.savedSelection=selection.toString();
    });
    await page.waitForTimeout(4700);
    const stability=await page.evaluate(()=>({same:window.savedAction===document.querySelector('#superAgentFocusActions button'),focused:document.activeElement===window.savedAction,selection:getSelection().toString()===window.savedSelection}));
    assert.deepEqual(stability,{same:true,focused:true,selection:true});
    mode='recovery';await page.locator('#superAgentRefresh').click();
    await page.waitForFunction(()=>document.querySelector('#superAgentFocusTitle').textContent.includes('尚未恢复'));
    assert.match(await page.locator('#superAgentFocusActions').innerHTML(),/data-super-action="retry"/);
    mode='completed';await page.locator('#superAgentRefresh').click();
    await page.waitForFunction(()=>document.querySelector('#superAgentStatus').textContent==='已完成');
    calls=[];await page.waitForTimeout(4700);
    assert.ok(calls.length>0);assert.ok(calls.every(path=>path.endsWith('/threads')),'已结束详情不应被其他活任务带着刷新');
    await page.locator('[data-super-thread="wb-smoke"]').focus();
    await page.evaluate(()=>{window.savedThread=document.querySelector('[data-super-thread="wb-smoke"]')});
    otherTitle='另一个任务已更新标题';
    await page.waitForFunction(()=>document.querySelector('[data-super-thread="other"]')?.textContent.includes('已更新标题'));
    assert.ok(await page.evaluate(()=>document.activeElement===window.savedThread && window.savedThread===document.querySelector('[data-super-thread="wb-smoke"]')),'其他任务变化不销毁当前列表焦点');
    await page.setViewportSize({width:375,height:812});
    await page.locator('#superAgentOpenSidebar').click();await page.locator('#sidebar.open').waitFor();
    await page.locator('[data-super-thread="wb-smoke"]').click();
    assert.equal(await page.locator('#sidebar').evaluate(el=>el.classList.contains('open')),false);
    await page.locator('#superAgentToggleInfo').click();await page.locator('#superAgentInfo').waitFor({state:'visible'});
    await page.locator('#superAgentCloseInfo').click();
    assert.equal(await page.locator('#superAgentInfo').isVisible(),false);
    await page.waitForTimeout(300);
    assert.ok(await page.locator('#sidebar').evaluate(el=>el.getBoundingClientRect().right<=1),'手机侧栏关闭后必须完全离开视口');
    await page.evaluate(()=>getSelection().removeAllRanges());
    await page.screenshot({path:path.join(out,'tasks-mobile-completed.png')});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({stability,keyedListFocus:true,terminalDetailRequests:0,mobileNavigation:true,pageErrors:errors,screenshots:out}));
  } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
