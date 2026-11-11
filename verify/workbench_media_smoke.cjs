// 工作台媒体实填回归：仅本机源码和 API 只读替身，禁止真实生成及后端写入。
// 运行：NODE_PATH 指向 Playwright；NAIBA_SMOKE_BASE 可改隔离实例地址。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8843';
const OUT = path.join(__dirname, 'workbench-media-results');
const params = [
  ...[['1','image'],['2','audio'],['3','video'],['14','image']].map(([node_id,kind]) => ({
    node_id, field_name: kind, field_type: kind, control: 'media', media: kind,
    node_title: `载入${kind}`, label: `${node_id} · 载入${kind} · ${kind} → references.${kind}_0（节点 200）\n输出 1 → secondary_${kind}（节点 201）；完整映射末尾`,
    default: 'None', media_optional: false,
  })),
  {node_id:'4', field_name:'text', field_type:'text', control:'multiline', role:'positive_prompt', node_title:'主提示词', label:'4 · 主提示词 · text → prompt（节点 200）', default:'原有正向提示词'},
  {node_id:'5', field_name:'text', field_type:'text', control:'multiline', node_title:'第二段提示词', label:'5 · 第二段提示词 · text → secondary_prompt（节点 200）', default:'第二段原值'},
  {node_id:'6', field_name:'duration', field_type:'number', node_title:'时长', label:'6 · 时长 · duration → seconds（节点 200）', default:5},
  {node_id:'7', field_name:'duration', field_type:'number', node_title:'第二时长', label:'7 · 第二时长 · duration → secondary_seconds（节点 200）', default:6},
  {node_id:'8', field_name:'width', field_type:'number', role:'resolution', node_title:'画幅', label:'8 · 画幅 · width → width（节点 200）', default:1024},
  {node_id:'9', field_name:'height', field_type:'number', role:'resolution', node_title:'画幅', label:'9 · 画幅 · height → height（节点 200）', default:768},
  {node_id:'10', field_name:'seed', field_type:'seed', role:'seed', node_title:'随机种子', label:'10 · 随机种子 · seed → seed（节点 200）', default:42},
  {node_id:'11', field_name:'enabled', field_type:'bool', node_title:'附加开关', label:'11 · 附加开关 · enabled → enabled（节点 200）', default:true},
  ...['12','13'].map(id => ({node_id:id, field_name:'lora_name', field_type:'text', control:'combo', role:'lora', node_title:'LoRA加载器', label:`${id} · LoRA加载器 · lora_name → model（节点 200）`, default:`model-${id}.safetensors`})),
];
const workflow = {
  id:'cw-workbench-media-smoke', name:'实填验收 · 图音视频与多端口工作流', workflow_fingerprint:'workbench-media-v1',
  node_count:params.length, param_count:params.length, params,
  visible_params:params.map(({node_id,field_name})=>({node_id,field_name})),
  param_order:params.map(({node_id,field_name})=>({node_id,field_name})),
  prompt:Object.fromEntries(params.map(slot=>[slot.node_id,{class_type:slot.node_title,inputs:{[slot.field_name]:slot.default}}])),
};
const results=[];

async function reachable(locator, label) {
  assert.equal(await locator.isVisible(),true,`${label} 可见`);
  await locator.scrollIntoViewIfNeeded();
  await locator.click({trial:true,timeout:3000});
  const box=await locator.boundingBox();
  const viewport=locator.page().viewportSize();
  assert.ok(box&&box.x>=-1&&box.x+box.width<=viewport.width+1,`${label} 不得横向越界：${JSON.stringify(box)}`);
}

async function checkGeometry(page, selector) {
  const report=await page.locator(selector).evaluate(root=>{
    const bounds=root.getBoundingClientRect(), overflow=[],small=[],touch=[];
    for(const node of root.querySelectorAll('*')){
      if(!node.checkVisibility({checkVisibilityCSS:true,checkOpacity:true})||node.closest('details:not([open])')&&!node.matches('summary'))continue;
      const rect=node.getBoundingClientRect(),css=getComputedStyle(node);
      if(!rect.width||!rect.height||node.tagName==='OPTION')continue;
      const name=node.id||`${node.tagName}.${node.className}${Object.keys(node.dataset).length?' '+JSON.stringify(node.dataset):''}`;
      if(rect.left<bounds.left-1||rect.right>bounds.right+1)overflow.push({name,left:rect.left,right:rect.right});
      if([...node.childNodes].some(child=>child.nodeType===3&&child.textContent.trim())&&parseFloat(css.fontSize)<12)small.push({name,size:css.fontSize});
      if(innerWidth<=760&&node.matches('button,summary,input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select')&&(rect.width<39.5||rect.height<39.5))touch.push({name,width:rect.width,height:rect.height});
    }
    return {width:bounds.width,overflow,small,touch};
  });
  assert.deepEqual(report.overflow,[],'实填内容不能越出页面');
  assert.deepEqual(report.small,[],'实填内容字号至少12px');
  assert.deepEqual(report.touch,[],'手机实填控件触控区至少40px');
  return report;
}

async function checkViewport(browser,width) {
  const page=await browser.newPage({viewport:{width,height:900}});
  const errors=[],writes=[],unexpected=[],mockedPreferences=[];
  let healthChecks=0;
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>localStorage.setItem('naibaOnboardingDismissed','1'));
  await page.route('**/*',route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!==new URL(BASE).origin)return route.abort();
    const reply=body=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    if(url.pathname==='/api/providers/models')return reply({models:[]});
    if(url.pathname==='/api/skills/scan')return reply({skills:[]});
    if(url.pathname==='/api/settings'&&request.method()==='POST'){
      const body=request.postDataJSON();
      if(Object.keys(body).length===1&&body.studio&&Object.keys(body.studio).every(key=>['view','page','pane'].includes(key))){
        mockedPreferences.push(body);
        return reply({settings:{studio:body.studio}});
      }
    }
    if(!['GET','HEAD'].includes(request.method())){
      writes.push(`${request.method()} ${url.pathname}`);
      return route.fulfill({status:403,contentType:'application/json',body:'{"error":"测试禁止后端写入"}'});
    }
    const responses={
      '/api/comfyui/health':{ok:true,url:'http://127.0.0.1:8188',devices:[]},
      '/api/comfyui/models':{models:[],loras:['model-12.safetensors','model-13.safetensors']},
      '/api/comfyui/workflows':{workflows:[workflow]},
      [`/api/comfyui/workflows/${workflow.id}`]:{workflow},
      '/api/comfyui/tasks':{tasks:[{id:'cf-media-smoke',workflow_name:'已有完成任务',status:'completed',result:{outputs:[]},progress:{done:1,total:1}}]},
      '/api/comfyui/batches':{batches:[]},
    };
    if(url.pathname==='/api/comfyui/health')healthChecks++;
    if(Object.hasOwn(responses,url.pathname))return reply(responses[url.pathname]);
    if(url.pathname.startsWith('/api/comfyui/')){
      unexpected.push(url.pathname);
      return reply({});
    }
    return route.continue();
  });
  try{
    await page.goto(BASE,{waitUntil:'networkidle'});
    await page.evaluate(async()=>{const studio=await import('/js/22-studio.js');studio.openStudioView();studio.setStudioPage('comfyui',{persist:false});});
    const pane=name=>page.locator(`#comfyuiPanes [data-comfyui-pane="${name}"]`);
    await pane('workflows').click();
    await page.locator('.comfyui-workflow-title').filter({hasText:workflow.name}).click();
    await page.locator('[data-comfyui-slot="4|text"]').waitFor({state:'visible'});
    assert.equal(await page.locator('[data-comfyui-slot="4|text"]').inputValue(),'原有正向提示词');
    await page.locator('[data-comfyui-slot="4|text"]').fill('单次草稿：返回仍保留');
    const connections=page.locator('.comfyui-param-connections');
    assert.equal(await connections.getAttribute('open'),null,'接线信息默认折叠');
    await connections.locator('summary').click();
    assert.deepEqual(await connections.locator('dd').allTextContents(),params.map(slot=>slot.label),'所有节点、字段、多输出和下游文本完整保留');
    assert.match((await connections.locator('dt').allTextContents()).join('\n'),/LoRA 模型（节点 12）/);
    await reachable(connections.locator('summary'),'参数接线说明');
    const single=await checkGeometry(page,'#studioPageComfyui');
    await page.screenshot({path:path.join(OUT,`single-mapping-${width}.png`)});

    // 页头菜单点开真实地址对话框，再取消；连接测试仅命中只读替身。
    const connectionSummary=page.locator('#comfyuiStatusbar .wb-media-menu > summary');
    await connectionSummary.click();
    await reachable(page.locator('#comfyuiEditUrl'),'改地址菜单');
    await page.locator('#comfyuiEditUrl').click();
    await page.locator('#comfyuiUrlDialog').waitFor({state:'visible'});
    await page.locator('#comfyuiUrlCancel').click();
    await connectionSummary.click();
    const beforeHealth=healthChecks;
    await page.locator('#comfyuiTestConn').click();
    await page.waitForTimeout(150);
    assert.ok(healthChecks>beforeHealth,'测试连接仍触发真实前端读取');

    // 页脚菜单向上展开，真实另存对话框只查看不提交。
    const actionSummary=page.locator('.comfyui-submit-row .wb-media-menu > summary');
    await actionSummary.click();
    await reachable(page.locator('#comfyuiSaveCurrentAs'),'另存工作流菜单');
    await page.locator('#comfyuiSaveCurrentAs').click();
    await page.locator('#comfyuiSaveAsDialog').waitFor({state:'visible'});
    assert.match(await page.locator('#comfyuiSaveAsList').innerText(),/单次草稿：返回仍保留/);
    await page.locator('#comfyuiSaveAsCancel').click();
    await actionSummary.click();
    await reachable(page.locator('#comfyuiOpenBatch'),'批量生成菜单');
    await page.locator('#comfyuiOpenBatch').click();
    await page.locator('[data-batch-select="workflow"]').selectOption(workflow.id);
    await page.locator('[data-batch-item="name"]').waitFor({state:'visible'});
    for(const [point,key] of [['prompt','4|text'],['duration','6|duration'],['resolution','8|width']])
      await page.locator(`[data-batch-select="mapping"][data-point="${point}"]`).selectOption(key);
    for(const [kind,key] of [['prompt','5|text'],['duration','7|duration'],['resolution','9|height'],['spare','11|enabled']]){
      await page.locator('[data-batch-action="add-extra"]').click();
      await page.locator('[data-batch-extra-kind]').last().selectOption(kind);
      await page.locator('[data-batch-extra-slot]').last().selectOption(key);
    }
    const row=page.locator('.cbm-items tbody > tr').first();
    const labels=await row.locator('td').evaluateAll(nodes=>nodes.map(node=>node.dataset.label));
    assert.equal(labels.length,15,'7个基础字段、4个媒体口、4个额外指认全部保留');
    assert.ok(labels.includes('名称')&&labels.includes('剧本')&&labels.includes('提示词')&&labels.includes('操作'));
    const mediaLabels=['图片 1 · 1','音频 1 · 2','视频 1 · 3','图片 2 · 14'];
    const mediaNumbers=['图 1','音 1','视频 1','图 2'];
    assert.deepEqual(labels.slice(1,5),mediaLabels,'手机混排素材口按种类分别编号');
    assert.deepEqual(await page.locator('.cbm-media-slot .cbm-no').allTextContents(),mediaNumbers,'配置区与手机编号一致');
    const headers=await page.locator('.cbm-items thead th').allTextContents();
    assert.deepEqual(headers.slice(1,5).map(text=>text.split('（')[0]),mediaNumbers,'桌面表头与手机编号一致');
    assert.deepEqual(await row.locator('[data-batch-tile]').evaluateAll(nodes=>nodes.map(node=>node.dataset.port)),
      ['0','1','2','3'],'媒体操作仍使用启用口的整体索引');
    if(width<=760){
      const displayedLabels=await row.locator('td').evaluateAll(nodes=>nodes.slice(1,5).map(node=>getComputedStyle(node,'::before').content));
      assert.deepEqual(displayedLabels,mediaLabels.map(label=>`"${label}"`),'手机实际显示正确媒体编号');
    }
    for(const cell of await row.locator('td').all()){
      assert.equal(await cell.isVisible(),true,`字段 ${await cell.getAttribute('data-label')} 可见`);
      if(width<=760){
        const control=cell.locator('input:enabled,textarea:enabled,select:enabled,button:enabled').filter({visible:true}).first();
        if(await control.count())await reachable(control,await cell.getAttribute('data-label'));
      }
    }
    await row.locator('[data-batch-item="name"]').fill('完整保留的条目名称');
    await row.locator('[data-batch-item="script"]').fill('剧本应当保留');
    await row.locator('[data-batch-item="prompt"]').fill('待清除的提示词');
    await row.locator('[data-batch-item="duration"]').fill('8');
    await row.locator('[data-batch-item="resolution"]').fill('1280');
    await row.locator('[data-batch-item="shots"]').fill('2');
    const clear=width<=760?page.locator('.cbm-mobile-tools [data-batch-action="clear-prompts"]'):page.locator('.cbm-th-clear');
    await reachable(clear,'清空本方案提示词');
    page.once('dialog',dialog=>{
      assert.equal(dialog.type(),'confirm');
      assert.match(dialog.message(),/清空这个方案的全部 1 条提示词.*剧本栏不动/);
      void dialog.accept();
    });
    await clear.click();
    assert.equal(await row.locator('[data-batch-item="prompt"]').inputValue(),'');
    assert.equal(await row.locator('[data-batch-item="script"]').inputValue(),'剧本应当保留');
    assert.equal(await row.locator('[data-batch-item="name"]').inputValue(),'完整保留的条目名称');
    assert.equal(await row.locator('[data-batch-item="duration"]').inputValue(),'8');
    assert.equal(await row.locator('[data-batch-item="shots"]').inputValue(),'2');
    const batch=width<=760?await checkGeometry(page,'#studioPageComfyui'):null;
    await row.locator('[data-batch-item="name"]').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(OUT,`batch-fields-${width}.png`)});
    await row.locator('[data-batch-action="remove-item"]').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(OUT,`batch-actions-${width}.png`)});

    // 结果管理入口做命中测试，不执行释放、打开目录或清除。
    await pane('tasks').click();
    const resultSummary=page.locator('.comfyui-tasks-actions > summary');
    await resultSummary.click();
    for(const id of ['comfyuiFreeModels','comfyuiOpenOutputs','comfyuiClearTasks'])await reachable(page.locator(`#${id}`),id);
    await page.keyboard.press('Escape');
    assert.equal(await resultSummary.locator('..').getAttribute('open'),null);
    await pane('run').click();
    assert.equal(await page.locator('[data-comfyui-slot="4|text"]').inputValue(),'单次草稿：返回仍保留');
    await pane('batch').click();
    assert.equal(await row.locator('[data-batch-item="name"]').inputValue(),'完整保留的条目名称');
    assert.deepEqual(errors,[],'无脚本异常');
    assert.deepEqual(writes,[],'不发送后端写入或生成请求');
    assert.deepEqual(unexpected,[],'无未定义造梦机接口');
    results.push({width,params:params.length,batchFields:labels,single,batch,errors,writes,mockedPreferences});
    console.log(`通过 ${width}px：${params.length}参数全映射、3处菜单、另存取消、${labels.length}批量字段、混排媒体编号一致、清空只影响提示词、往返草稿保持、零后端写入`);
  }catch(error){
    await page.screenshot({path:path.join(OUT,`failed-${width}.png`)});
    console.error({width,errors,writes,unexpected});
    throw error;
  }finally{await page.close();}
}

(async()=>{
  fs.mkdirSync(OUT,{recursive:true});
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{for(const width of (process.env.WB_WIDTHS||'1440,390,375,320').split(',').map(Number))await checkViewport(browser,width);}
  finally{fs.writeFileSync(path.join(OUT,'results.json'),JSON.stringify(results,null,2));await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
