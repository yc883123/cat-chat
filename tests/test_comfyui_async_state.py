"""执行真实前端函数，守住批量异步归属与素材口编号。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { buildBatchConvertRequest, BATCH_H3_REFERENCE_TEMPLATE } from './public/js/24a-comfyui-convert.js';
const defer=()=>{let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j;});return {promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
const timers=new Map(),messages=[],root={dataset:{comfyuiPane:'batch'}};
let timer=0,page='comfyui',open=true;
const context=vm.createContext({console,FormData,File,buildBatchConvertRequest,BATCH_H3_REFERENCE_TEMPLATE,
  setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id),
  setInterval:fn=>{timers.set(++timer,fn);return timer;},clearInterval:id=>timers.delete(id),
  document:{querySelector:()=>null,querySelectorAll:()=>[]},
  localStorage:{getItem:()=>null,setItem(){}},state:{providerModelCatalogs:{},bootstrap:{model_profiles:[]}},
  $:selector=>selector==='#studioPageComfyui'?root:null,toast:text=>messages.push(text),
  currentStudioPage:()=>page,isStudioOpen:()=>open,api:async()=>({})});
const source=fs.readFileSync('public/js/24-comfyui.js','utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
vm.runInContext(source+`
  renderBatch=()=>{};loadBatchHistories=async()=>{};
  globalThis.p={batch,startBatchPolling,stopBatchPolling,batchConvert,batchConvertAll,
    changeBatchMediaPorts,onBatchChange,onBatchClick,submitBatch,batchUploadTiles,loadBatchWorkflowDetail,
    setWait(fn){batchWaitStudioTask=fn;}};
`,context);
const p=context.p;
function fixture(){
  const ports=['1','2','3'].map(node_id=>({node_id,field_name:'image',checked:true}));
  const item={name:'条目1',script:'旧剧本',prompt:'',duration:'6',resolution:'',shots:1,
    files:{'0':{path:'/managed/a.png'},'1':{path:'/managed/b.png'},'2':{path:'/managed/c.png'}}};
  const scheme={id:'s1',workflow_id:'workflow1',detail:{params:ports.map(port=>({...port,media:'image',media_optional:true}))},
    mapping:{media_ports:ports},fixed:{},items:[item]};
  p.batch.draft={schemes:[scheme]};p.batch.convert={template:'',provider:'online:fixture',model:'fixture'};
  p.batch.converting.clear();return {scheme,item,ports};
}
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行前端逻辑")
class ComfyuiAsyncStateTests(unittest.TestCase):
    def run_case(self, body: str):
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-"], input=HARNESS + body,
            cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_workflow_detail_ignores_old_success_failure_clear_and_removed_scheme(self):
        self.run_case(r"""
for(const oldFails of [false,true]){
  const {scheme}=fixture(),old=defer(),next=defer();
  context.api=path=>path.endsWith('/old')?old.promise:next.promise;
  const first=p.loadBatchWorkflowDetail(scheme,'old');
  const second=p.loadBatchWorkflowDetail(scheme,'next');
  next.resolve({workflow:{id:'next',params:[]}});await second;
  if(oldFails)old.reject(new Error('旧请求失败'));else old.resolve({workflow:{id:'old',params:[]}});
  await first;
  assert.equal(scheme.workflow_id,'next');assert.equal(scheme.detail.id,'next');
  assert.equal(scheme.loading,false);assert.equal(p.batch.blockedMessage,'');
}
for(const remove of [false,true]){
  const {scheme}=fixture(),old=defer();context.api=()=>old.promise;
  const pending=p.loadBatchWorkflowDetail(scheme,'old');
  if(remove)p.batch.draft.schemes=[];else await p.loadBatchWorkflowDetail(scheme,'');
  const loading=scheme.loading;
  old.resolve({workflow:{id:'old',params:[]}});await pending;
  assert.equal(scheme.detail,null);assert.equal(scheme.mapping,null);
  assert.equal(scheme.loading,loading,'旧响应不能改写已失效方案的loading');
  if(!remove)assert.equal(scheme.loading,false,'清空选择应立即结束加载态');
}
""")

    def test_late_batch_responses_and_repeated_entry_cannot_stop_new_poll(self):
        self.run_case(r"""
const old=defer(),next=defer();let calls=0;
context.api=path=>{calls++;return path.endsWith('/old')?old.promise:next.promise;};
p.batch.view='run';p.batch.runId='old';p.startBatchPolling();p.startBatchPolling();
assert.equal(calls,1,'同一代请求未完成时重复进入不能叠加请求');
p.batch.runId='next';p.startBatchPolling();
next.resolve({batch:{id:'next',items:[{status:'running'}]}});await flush();
const activeTimer=p.batch.runTimer;
old.resolve({batch:{id:'old',items:[{status:'completed'}]}});await flush();
assert.equal(p.batch.runData.id,'next');assert.equal(p.batch.runTimer,activeTimer);
assert.ok(timers.has(activeTimer),'过期终态不得停止新轮询');
const again=defer();context.api=()=>again.promise;
timers.get(activeTimer)();p.stopBatchPolling();
again.resolve({batch:{id:'next',items:[{status:'completed'}]}});await flush();
assert.equal(p.batch.runData.items[0].status,'running','离开后返回的响应不得落地');
open=false;p.startBatchPolling();assert.equal(p.batch.runPoll,null,'关闭工作台不得新开轮询');
open=true;page='library';p.startBatchPolling();assert.equal(p.batch.runPoll,null);
page='comfyui';root.dataset.comfyuiPane='run';p.startBatchPolling();assert.equal(p.batch.runPoll,null);
""")

    def test_conversion_preserves_edited_or_removed_items_and_changed_inputs(self):
        self.run_case(r"""
for(const mutate of [
  ({item})=>{item.prompt='手填提示词';},({item})=>{item.script='新剧本';},
  ({item})=>{item.files['0']={path:'/managed/replaced.png'};},
  ({scheme})=>{scheme.mapping.media_ports.reverse();},
  ({scheme})=>{scheme.fixed.extra='new';},
  ()=>{p.batch.convert.template=BATCH_H3_REFERENCE_TEMPLATE;},
  ()=>{p.batch.convert.model='changed-model';},
  ({scheme})=>{scheme.detail={...scheme.detail};},
  ({scheme})=>{scheme.items=[];},
]){
  const f=fixture(),done=defer();context.api=async()=>({task:{id:'convert1'}});p.setWait(()=>done.promise);
  const pending=p.batchConvert(f.scheme,0);await flush();mutate(f);done.resolve('过期模型结果');
  assert.equal(await pending,false);assert.notEqual(f.item.prompt,'过期模型结果');
}
const f=fixture(),done=defer();context.api=async()=>({task:{id:'convert2'}});p.setWait(()=>done.promise);
const pending=p.batchConvert(f.scheme,0);await flush();
f.scheme.items.unshift({script:'前方新增',prompt:'',files:{}});done.resolve('正确条目结果');
assert.equal(await pending,true);assert.equal(f.item.prompt,'正确条目结果');assert.equal(f.scheme.items[0].prompt,'');
""")

    def test_convert_all_tracks_original_objects_after_row_deletion(self):
        self.run_case(r"""
const f=fixture(),first=defer(),sent=[];
const second={...f.item,script:'第二条',files:{},prompt:''},third={...f.item,script:'第三条',files:{},prompt:''};
f.scheme.items.push(second,third);let waits=0;
context.api=async(_path,options)=>{sent.push(options.body.user_request);return {task:{id:String(sent.length)}};};
p.setWait(()=>++waits===1?first.promise:Promise.resolve('后续转换结果'));
const pending=p.batchConvertAll();await flush();f.scheme.items.splice(1,1);first.resolve('首条结果');await pending;
assert.equal(sent.length,2);assert.match(sent[1],/第三条/);assert.equal(third.prompt,'后续转换结果');assert.equal(second.prompt,'');
""")

    def test_port_toggle_reorder_and_retarget_keep_correct_files_for_request_and_submission(self):
        self.run_case(r"""
const {scheme,item,ports}=fixture();
p.changeBatchMediaPorts(scheme,()=>{ports[0].checked=false;});
assert.deepEqual(JSON.parse(JSON.stringify(item.files)),{'0':{path:'/managed/b.png'},'1':{path:'/managed/c.png'}});
p.changeBatchMediaPorts(scheme,()=>{ports[0].checked=true;});
assert.equal(item.files['0'],undefined);assert.equal(item.files['1'].path,'/managed/b.png');
p.changeBatchMediaPorts(scheme,()=>{[ports[1],ports[2]]=[ports[2],ports[1]];});
assert.equal(item.files['1'].path,'/managed/c.png');assert.equal(item.files['2'].path,'/managed/b.png');
p.changeBatchMediaPorts(scheme,()=>{ports[1].node_id='4';});
assert.equal(item.files['1'],undefined);assert.equal(item.files['2'].path,'/managed/b.png');
const request=buildBatchConvertRequest(scheme.detail,scheme.mapping,item,p.batch.convert);
assert.deepEqual(request.images.map(file=>file.path),['/managed/b.png']);
let submitted;context.api=async(_path,options)=>{submitted=options.body;return {batch:{id:'submitted'}};};
open=false;await p.submitBatch();
assert.equal(submitted.schemes[0].items[0].media_inputs[0].node_id,'2');
assert.equal(submitted.schemes[0].items[0].media_inputs[0].path,request.images[0].path);
""")

    def test_upload_uses_selected_port_ordinal_and_revalidates_after_await(self):
        self.run_case(r"""
const {scheme,item,ports}=fixture();p.changeBatchMediaPorts(scheme,()=>{ports[0].checked=false;});
const tile={dataset:{scheme:scheme.id,item:'0',port:'0'}};
const file=new File(['bytes'],'image.png',{type:'image/png'});
context.api=async()=>({path:'/managed/replaced-b.png',name:'image.png'});
await p.batchUploadTiles([{tile,file}]);assert.equal(item.files['0'].path,'/managed/replaced-b.png');assert.equal(item.files['1'].path,'/managed/c.png');
const later=defer();context.api=()=>later.promise;
const pending=p.batchUploadTiles([{tile,file}]);await flush();
p.changeBatchMediaPorts(scheme,()=>{ports[1].node_id='changed';});
later.resolve({path:'/managed/stale.png'});await pending;
assert.equal(item.files['0'],undefined);assert.equal(item.files['1'].path,'/managed/c.png');
const f=fixture(),first=defer();let uploads=0;
context.api=()=>{uploads++;return uploads===1?first.promise:Promise.resolve({path:'/managed/second-upload.png'});};
const tile0={dataset:{scheme:f.scheme.id,item:'0',port:'0'}};
const tile1={dataset:{scheme:f.scheme.id,item:'0',port:'1'}};
const queued=p.batchUploadTiles([{tile:tile0,file},{tile:tile1,file}]);await flush();
p.changeBatchMediaPorts(f.scheme,()=>{f.ports[1].checked=false;});
first.resolve({path:'/managed/first-upload.png'});await queued;
assert.equal(uploads,1,'排队中原目标已失效时不能把旧序号用于另一口');
assert.equal(f.item.files['1'].path,'/managed/c.png');
""")


if __name__ == "__main__":
    unittest.main()
