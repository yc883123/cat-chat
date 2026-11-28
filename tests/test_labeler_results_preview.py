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
  +'retryTask,renderSupervisorSelects,syncConcurrencyUi,saveProviderLimit,ensureLabelerSettings,syncAnnotationsIfDue};',context);
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

    def test_approved_body_renders_verbatim_and_metadata_stays_searchable(self):
        self.run_case(r"""
seed();const body='  分镜一\r\n\r\n镜头二\n结尾  \n';
p.labeler.annotations[0].approved_txt=body;
p.labeler.annotations[0].description='已完成写入，assistant footer';
p.renderResultBody();
const desc=card().children.find(x=>x.className==='labeler-anno-desc');
assert.equal(desc.textContent,body);assert.equal(desc.title,'交付正文');
assert.ok(card().children.some(x=>x.className==='labeler-taglist'),'保留标签元数据');
assert.ok(!card().children.some(x=>x.textContent.includes('assistant footer')));
p.labeler.search='镜头二';p.renderResultBody();assertCard('按交付正文搜索');
p.labeler.search='cat';p.renderResultBody();assertCard('按元数据标签搜索');
p.labeler.search='assistant footer';p.renderResultBody();
assert.equal(card().children[0].textContent,'没有匹配的图片','不搜索已被正文替代的footer');
const css=fs.readFileSync('public/styles.css','utf8');
assert.match(css,/\.labeler-anno-desc\s*\{[^}]*white-space:\s*pre-wrap/,'卡片保留正文换行');
""")

    def test_body_and_same_length_metadata_change_redraw_signature(self):
        self.run_case(r"""
seed();p.labeler.annotations[0].approved_txt='第一版\n正文';p.renderResultBody();
const old=card(),sig=p.labeler.lastImageSignature;
p.renderResultBody();assert.equal(card(),old,'没变化无需重绘');
p.labeler.annotations[0].approved_txt='第二版\n正文';p.renderResultBody();
assert.notEqual(card(),old);assert.notEqual(p.labeler.lastImageSignature,sig);
assert.equal(card().children.find(x=>x.className==='labeler-anno-desc').textContent,'第二版\n正文');
const beforeTags=card();p.labeler.annotations[0].tags=['dog'];p.renderResultBody();
assert.notEqual(card(),beforeTags,'标签数相同也要重绘元数据');
assert.equal(card().children.find(x=>x.className==='labeler-taglist').children[0].textContent,'dog');
p.labeler.annotations[0].approved_txt='';p.renderResultBody();
assert.equal(card().children.find(x=>x.className==='labeler-anno-desc').textContent,'','空正文不可回退');
p.labeler.annotations[0].approved_txt=null;p.renderResultBody();
assert.equal(card().children.find(x=>x.className==='labeler-anno-desc').textContent,'一只猫坐在窗台上');
assert.ok(card().children.some(x=>x.textContent.includes('历史标注：未保存交付正文')));
p.labeler.search='窗台';p.renderResultBody();assertCard('历史描述仍可搜索');
""")

    def test_tag_result_cards_use_approved_body_and_explain_legacy(self):
        self.run_case(r"""
seed();p.labeler.resultTab='tags';p.labeler.tags=[{tag:'cat',count:2}];p.labeler.activeTag='cat';
p.labeler.task.input={config:{write_txt:true}};
p.labeler.tagMatches=[{id:1,name:'a.png',tags:['cat'],description:'assistant footer',approved_txt:'正文\n两行'},
  {id:2,name:'b.png',source_task_id:'tg-test',tags:['cat'],description:'历史描述',approved_txt:null}];
p.renderResultBody();
const approved=resultBody.children[1],legacy=resultBody.children[2];
assert.equal(approved.children.find(x=>x.className==='labeler-anno-desc').textContent,'正文\n两行');
assert.equal(legacy.children.find(x=>x.className==='labeler-anno-desc').textContent,'历史描述');
assert.ok(legacy.children.some(x=>x.textContent.includes('历史标注：未保存交付正文')));
p.labeler.task.input.config.write_txt=false;p.renderResultBody();
assert.ok(!resultBody.children[2].children.some(x=>x.textContent.includes('历史标注：')),'普通非TXT标注不误报历史');
""")

    def test_running_increment_failure_retries_without_advancing_checkpoint(self):
        self.run_case(r"""
seed();let calls=0,fail=true;
apiImpl=async()=>{calls++;if(fail)throw Error('临时断线');
  return{annotations:[{id:1,approved_txt:'恢复后的正文\n第二行'}],total:1};};
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
(async()=>{
  const old=p.labeler.annotations;p.syncAnnotationsIfDue('tg-test',p.labeler.items);await tick();
  assert.equal(calls,1);assert.equal(p.labeler.annotations,old);
  assert.equal(p.labeler.resultsSyncDoneCount,0,'失败不推进完成数');
  assert.equal(p.labeler.resultsSyncTaskId,'');assert.equal(p.labeler.resultsSyncPending,null);
  fail=false;p.syncAnnotationsIfDue('tg-test',p.labeler.items);await tick();
  assert.equal(calls,2,'同完成数重试');assert.equal(p.labeler.resultsSyncDoneCount,1);
  assert.equal(p.labeler.annotations[0].approved_txt,'恢复后的正文\n第二行');
  p.syncAnnotationsIfDue('tg-test',p.labeler.items);await tick();assert.equal(calls,2,'成功后不重复');
})().catch(err=>{console.error(err);process.exit(1);});
""")

    def test_running_increment_inflight_is_not_duplicated_and_new_count_remains_due(self):
        self.run_case(r"""
seed();let calls=0,finish;
apiImpl=()=>{calls++;return new Promise(resolve=>{finish=resolve;});};
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
(async()=>{
  p.syncAnnotationsIfDue('tg-test',p.labeler.items);
  p.syncAnnotationsIfDue('tg-test',p.labeler.items);assert.equal(calls,1);
  p.labeler.items.push({item_id:'i2',state:'succeeded',annotation_id:2});
  p.syncAnnotationsIfDue('tg-test',p.labeler.items);assert.equal(calls,1,'请求未完成不重复');
  assert.equal(p.labeler.resultsSyncDoneCount,0,'在途不推进检查点');
  finish({annotations:[{id:1,approved_txt:'第一张'}],total:1});await tick();
  assert.equal(p.labeler.resultsSyncDoneCount,1,'只确认本次开始时完成数');
  p.syncAnnotationsIfDue('tg-test',p.labeler.items);assert.equal(calls,2,'新增完成仍需补拉');
  finish({annotations:[{id:1},{id:2,approved_txt:'第二张'}],total:2});await tick();
  assert.equal(p.labeler.resultsSyncDoneCount,2);
  p.syncAnnotationsIfDue('tg-test',p.labeler.items);assert.equal(calls,2);
})().catch(err=>{console.error(err);process.exit(1);});
""")

    def test_running_increment_old_task_response_cannot_advance_new_checkpoint(self):
        self.run_case(r"""
seed();const finishes=[];let calls=0;
apiImpl=()=>{calls++;return new Promise(resolve=>finishes.push(resolve));};
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
(async()=>{
  p.syncAnnotationsIfDue('tg-test',p.labeler.items);
  p.labeler.taskId='new-task';p.labeler.detailToken++;
  p.labeler.items=[{item_id:'new',state:'succeeded',annotation_id:9}];
  p.labeler.annotations=[];p.syncAnnotationsIfDue('new-task',p.labeler.items);
  const pending=p.labeler.resultsSyncPending;assert.equal(calls,2);
  finishes[0]({annotations:[{id:1,approved_txt:'旧正文'}],total:1});await tick();
  assert.equal(p.labeler.annotations.length,0);assert.equal(p.labeler.resultsSyncDoneCount,0);
  assert.equal(p.labeler.resultsSyncTaskId,'');assert.equal(p.labeler.resultsSyncPending,pending);
  p.syncAnnotationsIfDue('new-task',p.labeler.items);assert.equal(calls,2,'旧finally不能清新请求锁');
  finishes[1]({annotations:[{id:9,approved_txt:'新正文'}],total:1});await tick();
  assert.equal(p.labeler.resultsSyncTaskId,'new-task');assert.equal(p.labeler.resultsSyncDoneCount,1);
  assert.equal(p.labeler.annotations[0].approved_txt,'新正文');
})().catch(err=>{console.error(err);process.exit(1);});
""")

    def test_running_increment_revisiting_same_task_uses_new_generation(self):
        self.run_case(r"""
seed();const finishes=[];let calls=0;
apiImpl=()=>{calls++;return new Promise(resolve=>finishes.push(resolve));};
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
(async()=>{
  p.syncAnnotationsIfDue('tg-test',p.labeler.items);
  p.labeler.detailToken++;p.syncAnnotationsIfDue('tg-test',p.labeler.items);assert.equal(calls,2);
  finishes[1]({annotations:[{id:1,approved_txt:'当前正文'}],total:1});await tick();
  const checkpoint=p.labeler.resultsSyncDetailToken;
  finishes[0]({annotations:[{id:1,approved_txt:'上次访问的正文'}],total:1});await tick();
  assert.equal(p.labeler.annotations[0].approved_txt,'当前正文');
  assert.equal(p.labeler.resultsSyncDetailToken,checkpoint);
  p.labeler.detailToken++;p.syncAnnotationsIfDue('tg-test',p.labeler.items);
  assert.equal(calls,3,'同完成数的新访问仍需要拉取');
  finishes[2]({annotations:[{id:1,approved_txt:'再次访问正文'}],total:1});await tick();
  assert.equal(p.labeler.resultsSyncDetailToken,p.labeler.detailToken);
})().catch(err=>{console.error(err);process.exit(1);});
""")

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
        """最后运行态必须变为成功或失败，不能只更新批次状态。"""
        self.run_case(r"""
(async()=>{
for(const terminal of ['succeeded','failed']){
  seed();p.labeler.items=[{item_id:'i1',name:'a.png',state:'running',annotation_id:null}];
  p.labeler.annotations=[];p.labeler.annotationTotal=0;p.renderResultBody();
  let finish;
  apiImpl=async(url)=>{
    if(url.includes('/items')){
      assert.match(url,/offset=0&/);
      return new Promise(resolve=>{finish=()=>resolve({total:1,items:[{
        item_id:'i1',name:'a.png',state:terminal,annotation_id:terminal==='succeeded'?1:null,
        error:terminal==='failed'?'供应商拒绝':'',
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
  assert.ok(texts.includes(terminal==='succeeded'?'完成正文':'供应商拒绝'));
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
