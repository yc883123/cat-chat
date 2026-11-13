"""贴签匠逐图结果预览：批次转终态后不能把已渲染的「按图」结果清掉。

远端反馈（2026-11-10）：打标过程中逐图卡片（标签/描述/TXT 状态）看得见，
批次一结束结果区就变成「还没有逐图结果」并停在那里。根因是终态详情接口不带
items，前端先清空列表渲染空态、再由 loadTaskItems 补回来；补回来的签名与上一轮
一致，被懒加载签名 diff 挡掉，空态就一直挂着。

这里用 Node + vm 跑真实的 23-labeler.js 函数：不加载浏览器也能钉住渲染行为。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function node(tag='div'){
  const el={tagName:tag,children:[],className:'',textContent:'',title:'',hidden:false,disabled:false,
    value:'',scrollTop:0,scrollLeft:0,
    dataset:{},style:{},classList:{add(){},remove(){},toggle(){},contains(){return false}},
    append(...kids){for(const kid of kids)el.children.push(kid);},
    replaceChildren(...kids){el.children=[...kids];},
    addEventListener(){},setAttribute(){},removeAttribute(){},querySelector(){return null;}};
  return el;
}
const resultBody=node('div'),hint=node('p');
const elements={'#labelerResultBody':resultBody,'#labelerResultsHint':hint};
let apiImpl=async()=>({});
const context=vm.createContext({
  console,$:key=>elements[key]||null,$$:()=>[],
  api:(url,opts)=>apiImpl(url,opts),
  state:{providerModelCatalogs:{}},onProvidersChanged(){},onStudioPageChange(){},currentStudioPage:()=>'',setTimeout,clearTimeout,setInterval,clearInterval,
  document:{addEventListener(){},createElement:tag=>node(tag),createTextNode:text=>({textContent:text})},
  window:{addEventListener(){},matchMedia:()=>({matches:false})},
});
const catalogSource=fs.readFileSync('public/js/07-models-agents.js','utf8');
const catalogFunction=name=>{const start=catalogSource.search(new RegExp('^export (?:async )?function '+name+'\\(', 'm'));return catalogSource.slice(start,catalogSource.indexOf('\n}',start)+2).replace(/^export /,'');};
vm.runInContext('const catalogGenerations=new Map(),catalogRequests=new Map();\n'+catalogFunction('invalidateModelCatalogs')+'\n'+catalogFunction('loadModelCatalog'),context);
const source=fs.readFileSync('public/js/23-labeler.js','utf8')
  .replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
vm.runInContext(source+'\nglobalThis.p={labeler,renderResultBody,pollTask,loadTaskItems,loadTaskResults,loadNextAnnotations,renderTask,'
  +'retryTask,renderSupervisorSelects,syncConcurrencyUi,saveProviderLimit,ensureLabelerSettings};',context);
const p=context.p;
function seed(){
  p.labeler.taskId='tg-test';
  p.labeler.task={status:'running',items_total:1,progress:{done:1,total:1}};
  p.labeler.items=[{item_id:'i1',state:'succeeded',annotation_id:1,
    relative_path:'d/a0.png',name:'a0.png',txt_state:'written',txt_path:'d/a0.txt',txt_chars:16}];
  p.labeler.annotations=[{id:1,tags:['cat'],description:'一只猫坐在窗台上'}];
  p.labeler.annotationTotal=1;
  p.renderResultBody();
}
function card(){return resultBody.children[0];}
function assertCard(label){
  assert.equal(resultBody.children.length,1,label+'：应只有一张卡');
  assert.equal(card().className,'labeler-anno',label+'：应渲染逐图卡而不是空态');
  assert.equal(card().children[0].children[0].textContent,'d/a0.png',label+'：卡片标题');
}
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行贴签匠前端")
class LabelerResultsPreviewTests(unittest.TestCase):
    def run_case(self, body: str) -> None:
        result = subprocess.run([shutil.which("node"), "-"], input=HARNESS + body,
                                cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_terminal_wipe_then_restore_redraws_cards(self):
        """终态清空 items 后补拉回来，必须重绘逐图卡（签名 diff 不能挡住）。"""
        self.run_case(r"""
seed();assertCard('打标中');
p.labeler.items=[];p.renderResultBody();
assert.equal(resultBody.children[0].children[0].textContent,'还没有逐图结果');
seed();  // 补拉回来（同样的 items / annotations）
assertCard('补拉后');
""")

    def test_poll_terminal_keeps_rendered_items(self):
        """轮询拿到终态详情（items 为空）时，不清掉已渲染的逐图列表。"""
        self.run_case(r"""
seed();
apiImpl=async(url)=>{
  if(url.includes('/annotations'))return{annotations:[{id:1,tags:['cat'],description:'一只猫坐在窗台上'}],total:1};
  if(url.includes('/items')){
    assert.match(url,/offset=0&/,'终态应重取已有图片，不是从旧列表末尾续页');
    return{items:[{item_id:'i1',state:'succeeded',annotation_id:1,
      relative_path:'d/a0.png',txt_state:'written',txt_path:'d/a0.txt',txt_chars:16}],total:1,count:1};
  }
  if(url.includes('/api/labeler/tasks/tg-test'))return{task:{id:'tg-test',status:'done',
    progress:{done:1,total:1,failed:0,uncertain:0,cancelled:0},items:[],items_total:1}};
  if(url.includes('/api/labeler/tasks'))return{tasks:[]};
  throw new Error('unexpected request '+url);
};
(async()=>{await p.pollTask();
  assert.equal(p.labeler.items.length,1,'终态轮询不该清空 items');
  assertCard('转终态后');
})().catch((err)=>{console.error(err);process.exit(1);});
""")

    def test_poll_refreshes_last_running_image_to_terminal(self):
        """最后运行态必须变为成功、失败或人工检查，不能只更新批次状态。"""
        self.run_case(r"""
(async()=>{
for(const terminal of ['succeeded','failed','human_check']){
  seed();p.labeler.items=[{item_id:'i1',name:'a.png',state:'running',annotation_id:null}];
  p.labeler.annotations=[];p.labeler.annotationTotal=0;p.renderResultBody();
  let finish;
  apiImpl=async(url)=>{
    if(url.includes('/items')){
      assert.match(url,/offset=0&/);
      return new Promise(resolve=>{finish=()=>resolve({total:1,items:[{
        item_id:'i1',name:'a.png',state:terminal,annotation_id:terminal==='succeeded'?1:null,
        error:terminal==='failed'?'供应商拒绝':'',
        candidate:terminal==='human_check'?{description:'待检查候选'}:null,
      }]});});
    }
    if(url.includes('/annotations'))return{annotations:[{id:1,tags:['cat'],description:'完成正文'}],total:1};
    if(url.endsWith('/tg-test'))return{task:{id:'tg-test',status:'done',items:[],progress:{total:1}}};
    return{tasks:[]};
  };
  const loading=p.pollTask();await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(p.labeler.items[0].state,'running','重取期间保留旧卡，不闪空态');
  finish();await loading;
  assert.equal(p.labeler.items[0].state,terminal);
  assert.equal(p.labeler.items[0].annotation_id,terminal==='succeeded'?1:null);
  const texts=card().children.map(x=>x.textContent).join('|');
  assert.ok(texts.includes(terminal==='succeeded'?'完成正文':terminal==='failed'?'供应商拒绝':'待检查候选'));
  assert.equal(p.labeler.pollTimer,null);
}
})().catch(err=>{console.error(err);process.exit(1);});
""")

    def test_refresh_loaded_pages_replaces_by_id_and_keeps_scroll(self):
        self.run_case(r"""
seed();
p.labeler.items=Array.from({length:750},(_,i)=>({item_id:'i'+i,state:'running',name:String(i)}));
p.labeler.imageResultLimit=600;
resultBody.scrollTop=240;
const replace=resultBody.replaceChildren;
resultBody.replaceChildren=(...kids)=>{replace(...kids);resultBody.scrollTop=0;};
const offsets=[];
apiImpl=async(url)=>{
  const offset=Number(new URL(url,'http://fixture').searchParams.get('offset'));offsets.push(offset);
  return{total:750,items:Array.from({length:Math.min(500,750-offset)},(_,i)=>({
    item_id:'i'+(offset+i),state:'succeeded',name:String(offset+i)}))};
};
(async()=>{
  assert.equal(await p.loadTaskItems('tg-test',{refresh:true}),true);
  assert.deepEqual(offsets,[0,500]);assert.equal(p.labeler.items.length,750);
  assert.equal(new Set(p.labeler.items.map(x=>x.item_id)).size,750);
  assert.ok(p.labeler.items.every(x=>x.state==='succeeded'));
  assert.equal(p.labeler.imageResultLimit,600);assert.equal(resultBody.scrollTop,240);
  apiImpl=async()=>({total:751,items:[{item_id:'i749',state:'failed'},{item_id:'i750',state:'succeeded'}]});
  await p.loadTaskItems('tg-test');
  assert.equal(p.labeler.items.length,751);assert.equal(p.labeler.items[749].state,'failed');
})().catch(err=>{console.error(err);process.exit(1);});
""")

    def test_refresh_failure_keeps_old_rows_and_late_response_is_ignored(self):
        self.run_case(r"""
seed();const old=p.labeler.items;
p.labeler.items=Array.from({length:501},(_,i)=>({item_id:'i'+i,state:'running'}));
const before=p.labeler.items;
apiImpl=async(url)=>{
  if(url.includes('offset=0&'))return{total:501,items:before.slice(0,500).map(x=>({...x,state:'succeeded'}))};
  throw Error('临时断线');
};
(async()=>{
  assert.equal(await p.loadTaskItems('tg-test',{refresh:true}),false);
  assert.equal(p.labeler.items,before,'第二页失败不能落下半份刷新');
  let finish;apiImpl=()=>new Promise(resolve=>{finish=resolve;});
  const pending=p.loadTaskItems('tg-test',{refresh:true});
  p.labeler.taskId='other';p.labeler.items=[{item_id:'other',state:'queued'}];
  finish({total:1,items:old});await pending;
  assert.equal(p.labeler.items[0].item_id,'other','切批次后不接旧结果');
  p.labeler.taskId='tg-test';
  const previousVisit=p.loadTaskItems('tg-test',{refresh:true});
  p.labeler.detailToken+=1;finish({total:1,items:old});await previousVisit;
  assert.equal(p.labeler.items[0].item_id,'other','离开后再回同批次也拒绝旧代次');
})().catch(err=>{console.error(err);process.exit(1);});
""")

    def test_running_poll_keeps_loaded_tail_pages(self):
        self.run_case(r"""
seed();p.labeler.items=Array.from({length:750},(_,i)=>({item_id:'i'+i,state:'running',name:String(i)}));
apiImpl=async()=>({task:{id:'tg-test',status:'running',items_total:750,
  items:Array.from({length:500},(_,i)=>({item_id:'i'+i,state:'failed',name:String(i)}))}});
(async()=>{await p.pollTask();
  assert.equal(p.labeler.items.length,750,'运行轮询不能丢弃用户加载过的尾页');
  assert.equal(p.labeler.items[0].state,'failed');assert.equal(p.labeler.items[749].state,'running');
})().catch(err=>{console.error(err);process.exit(1);});
""")

    def test_late_running_annotations_cannot_undo_terminal_refresh(self):
        self.run_case(r"""
seed();let finishOld;apiImpl=()=>new Promise(resolve=>{finishOld=resolve;});
p.labeler.items.push({item_id:'i2',state:'running',annotation_id:null});
const previous=p.loadTaskResults('tg-test');
apiImpl=async(url)=>{
  if(url.includes('/annotations'))return{annotations:[{id:1,tags:[],description:'第一张'},
    {id:2,tags:[],description:'最终正文'}],total:2};
  if(url.includes('/items'))return{items:[{item_id:'i1',state:'succeeded',annotation_id:1},
    {item_id:'i2',state:'succeeded',annotation_id:2}],total:2};
  if(url.endsWith('/tg-test'))return{task:{id:'tg-test',status:'done',items:[],progress:{done:2,total:2}}};
  return{tasks:[]};
};
(async()=>{await p.pollTask();finishOld({annotations:[{id:1,tags:[],description:'第一张'}],total:1});await previous;
  assert.equal(p.labeler.annotations.length,2);
  assert.equal(p.labeler.annotations[1].description,'最终正文');assert.equal(p.labeler.pollTimer,null);
})().catch(err=>{console.error(err);process.exit(1);});
""")

    def test_annotation_page_cannot_append_to_a_new_refresh(self):
        self.run_case(r"""
seed();p.labeler.annotationTotal=2;let finishPage;
apiImpl=()=>new Promise(resolve=>{finishPage=resolve;});const page=p.loadNextAnnotations();
apiImpl=async()=>({annotations:[{id:1,tags:[],description:'新快照'}],total:1});
(async()=>{await p.loadTaskResults('tg-test');finishPage({annotations:[{id:2,description:'旧页'}]});await page;
  assert.equal(p.labeler.annotations.length,1);assert.equal(p.labeler.annotations[0].description,'新快照');
})().catch(err=>{console.error(err);process.exit(1);});
""")

    def test_terminal_annotation_failure_keeps_old_output_and_retries(self):
        self.run_case(r"""
seed();const old=p.labeler.annotations;context.currentStudioPage=()=> 'labeler';
let fail=true;
apiImpl=async(url)=>{
  if(url.includes('/annotations')){
    if(fail)throw Error('临时断线');
    return{annotations:[{id:1,description:'重试后结果'}],total:1};
  }
  if(url.includes('/items'))return{items:[{item_id:'i1',state:'succeeded',annotation_id:1}],total:1};
  if(url.endsWith('/tg-test'))return{task:{id:'tg-test',status:'done',items:[],progress:{done:1,total:1}}};
  return{tasks:[]};
};
(async()=>{try{
  await p.pollTask();assert.equal(p.labeler.annotations,old,'收尾网络失败保留已展示结果');
  assert.ok(p.labeler.pollTimer,'终态收尾未完成，应恢复轮询重试');
  fail=false;await p.pollTask();
  assert.equal(p.labeler.annotations[0].description,'重试后结果');assert.equal(p.labeler.pollTimer,null);
}finally{clearInterval(p.labeler.pollTimer);}
})().catch(err=>{console.error(err);process.exit(1);});
""")
