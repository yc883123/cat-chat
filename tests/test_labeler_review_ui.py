"""贴签匠监督配置、单键额度保存及继续入口的真实前端回归。"""
from __future__ import annotations

import shutil
import subprocess
import unittest

from tests.test_labeler_results_preview import HARNESS, ROOT


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行贴签匠前端")
class LabelerReviewUiTests(unittest.TestCase):
    def run_case(self, body: str) -> None:
        script = HARNESS + "\n(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exit(1);});"
        result = subprocess.run([shutil.which("node"), "-"], input=script,
                                cwd=ROOT, text=True, encoding="utf-8",
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_supervisor_ignores_late_provider_catalog(self):
        self.run_case(r"""
elements['#labelerSupervisorProvider']=node('select');
elements['#labelerSupervisorModel']=node('select');
context.state.bootstrap={model_profiles:[{model_key:'A'},{model_key:'B'}]};
const requests={};apiImpl=(url,options)=>new Promise(resolve=>{requests[options.body.model_key]=resolve;});
p.labeler.supervisor={providerKey:'A',modelName:'a'};
const a=p.renderSupervisorSelects();
p.labeler.supervisor={providerKey:'B',modelName:''};
const b=p.renderSupervisorSelects();requests.B({models:[{id:'b'}]});await b;
requests.A({models:[{id:'a'}]});await a;
assert.equal(p.labeler.supervisor.providerKey,'B');assert.equal(p.labeler.supervisor.modelName,'b');
assert.equal(elements['#labelerSupervisorModel'].value,'b');
""")

    def test_supervisor_late_failure_does_not_replace_new_hint_and_can_retry(self):
        self.run_case(r"""
elements['#labelerSupervisorProvider']=node('select');
elements['#labelerSupervisorModel']=node('select');
elements['#labelerTaskHint']=node();
context.state.bootstrap={model_profiles:[{model_key:'A'},{model_key:'B'}]};
let fail;apiImpl=(url,options)=>options.body.model_key==='A'
  ?new Promise((resolve,reject)=>{fail=reject;}):Promise.resolve({models:[{id:'b'}]});
p.labeler.supervisor={providerKey:'A',modelName:''};const a=p.renderSupervisorSelects();
p.labeler.supervisor={providerKey:'B',modelName:''};await p.renderSupervisorSelects();
elements['#labelerTaskHint'].textContent='最新操作提示';fail(Error('旧目录错误'));await a;
assert.equal(elements['#labelerTaskHint'].textContent,'最新操作提示');
assert.equal(p.labeler.supervisor.modelName,'b');
assert.equal(context.state.providerModelCatalogs.A,undefined,'失败不能被永久缓存为空目录');
apiImpl=async()=>({models:[{id:'a-recovered'}]});
p.labeler.supervisor={providerKey:'A',modelName:''};await p.renderSupervisorSelects();
assert.equal(p.labeler.supervisor.modelName,'a-recovered');
""")

    def test_supervisor_old_generation_cannot_win_same_provider(self):
        self.run_case(r"""
elements['#labelerSupervisorProvider']=node('select');elements['#labelerSupervisorModel']=node('select');
context.state.bootstrap={model_profiles:[{model_key:'A'}]};
const replies=[];apiImpl=()=>new Promise(resolve=>replies.push(resolve));
p.labeler.supervisor={providerKey:'A',modelName:''};const first=p.renderSupervisorSelects();
context.invalidateModelCatalogs(['A']);
const second=p.renderSupervisorSelects();replies[1]({models:[{id:'new'}]});await second;
replies[0]({models:[{id:'old'}]});await first;
assert.equal(p.labeler.supervisor.modelName,'new');
assert.equal(context.state.providerModelCatalogs.A[0].id,'new','旧代次也不能污染下次重绘的目录缓存');
""")

    def test_provider_limits_patch_isolated_keys_and_late_full_response(self):
        self.run_case(r"""
const requests=[];const replies=[];
apiImpl=(url,options)=>new Promise(resolve=>{requests.push(options.body);replies.push(resolve);});
const a=p.saveProviderLimit('A',1);const b=p.saveProviderLimit('B',2);await Promise.resolve();
assert.equal(JSON.stringify(requests),JSON.stringify([
  {provider_limits_patch:{A:1}},{provider_limits_patch:{B:2}}]));
replies[1]({provider_limits:{A:3,B:2}});await b;
replies[0]({provider_limits:{A:1,B:3}});await a;
assert.equal(p.labeler.providerLimits.A,1);assert.equal(p.labeler.providerLimits.B,2);
assert.equal(p.labeler.providerLimitPending.size,0);
""")

    def test_same_provider_saves_in_order_and_failure_restores_last_success(self):
        self.run_case(r"""
elements['#labelerTaskHint']=node();
const calls=[];const replies=[];
apiImpl=(url,options)=>new Promise((resolve,reject)=>{calls.push(options.body);replies.push({resolve,reject});});
const first=p.saveProviderLimit('A',1),second=p.saveProviderLimit('A',2);
await Promise.resolve();assert.equal(calls.length,1);assert.equal(p.labeler.providerLimitPending.get('A').value,2);
replies[0].resolve({provider_limits:{A:1}});await first;
await Promise.resolve();assert.equal(calls.length,2);
assert.equal(calls[1].provider_limits_patch.A,2);
replies[1].reject(Error('无法保存'));await second;
assert.equal(p.labeler.providerLimits.A,1);assert.equal(p.labeler.providerLimitPending.has('A'),false);
assert.match(elements['#labelerTaskHint'].textContent,/保存失败/);
apiImpl=async()=>({provider_limits:{A:2}});await p.saveProviderLimit('A',2);
assert.equal(p.labeler.providerLimits.A,2,'失败后仍可继续保存');
""")

    def test_settings_read_started_before_save_cannot_undo_success(self):
        self.run_case(r"""
let finishRead;apiImpl=(url,options)=>options?.method==='POST'
  ?Promise.resolve({provider_limits:{A:1}}):new Promise(resolve=>{finishRead=resolve;});
const reading=p.ensureLabelerSettings();await p.saveProviderLimit('A',1);
finishRead({provider_limits:{A:3}});await reading;
assert.equal(p.labeler.providerLimits.A,1);
""")

    def test_continue_visibility_uses_server_capability(self):
        self.run_case(r"""
for(const id of ['#labelerContinue','#labelerRetryModelFailed']){elements[id]=node('button');elements[id].hidden=true;}
p.renderTask({status:'done',can_continue:true,can_retry_failed:false,progress:{total:1,failed:1}});
assert.equal(elements['#labelerContinue'].hidden,false);
assert.equal(elements['#labelerContinue'].disabled,false);
assert.equal(elements['#labelerRetryModelFailed'].hidden,true);
p.renderTask({status:'running',can_continue:false,progress:{total:1}});
assert.equal(elements['#labelerContinue'].hidden,true);
""")

    def test_continue_failure_reenables_button_and_double_click_is_blocked(self):
        self.run_case(r"""
elements['#labelerContinue']=node('button');elements['#labelerTaskHint']=node();
p.labeler.taskId='task';p.labeler.task={id:'task',status:'done',can_continue:true,progress:{failed:1}};
let reject;let calls=0;apiImpl=()=>{calls++;return new Promise((resolve,fail)=>{reject=fail;});};
const first=p.retryTask('unfinished');await p.retryTask('unfinished');
assert.equal(calls,1);assert.equal(elements['#labelerContinue'].disabled,true);
reject(Error('临时断线'));await first;
assert.equal(elements['#labelerContinue'].hidden,false);assert.equal(elements['#labelerContinue'].disabled,false);
apiImpl=async(url)=>url.endsWith('/retry')?{task:{id:'next',status:'queued',items:[]}}:{tasks:[]};
await p.retryTask('unfinished');assert.equal(p.labeler.taskId,'next');
assert.equal(p.labeler.retryBusy,false);
""")

    def test_reused_unknown_worker_does_not_show_slot_zero(self):
        self.run_case(r"""
seed();Object.assign(p.labeler.items[0],{worker_slot:-1,worker_model_name:'original-model',supervisor_decision:'reused'});
p.renderResultBody();const text=JSON.stringify(resultBody.children);
assert.match(text,/原槽位未知/);assert.match(text,/original-model/);assert.doesNotMatch(text,/打标员 #0/);
p.labeler.items[0].worker_slot=0;p.renderResultBody();
assert.match(JSON.stringify(resultBody.children),/打标员 #1/);
""")

    def test_failed_retry_preserves_completed_text_and_rejects_old_results(self):
        self.run_case(r"""
seed();elements['#labelerTaskHint']=node();
p.labeler.task={id:'tg-test',status:'done',can_retry_failed:true,progress:{done:1,total:2,failed:1}};
p.labeler.items.push({item_id:'i2',name:'failed.png',state:'failed',error:'首次失败'});
const resumed={id:'tg-test',status:'running',progress:{done:1,total:2},items_total:2,
  items:[{item_id:'i2',name:'failed.png',state:'queued',error:''}]};
let oldItems,oldResults,finishBatches;let annotationReads=0;
apiImpl=(url)=>{
  if(url.endsWith('/retry'))return Promise.resolve({task:resumed,skipped_succeeded:1});
  if(url.endsWith('/tasks'))return new Promise(resolve=>{finishBatches=resolve;});
  if(url.includes('/items?'))return new Promise(resolve=>{oldItems=resolve;});
  if(url.includes('/annotations?')){
    if(++annotationReads===1)return new Promise(resolve=>{oldResults=resolve;});
    return Promise.resolve({annotations:[{id:1,description:'一只猫坐在窗台上'}],total:1});
  }
  if(url.endsWith('/tg-test'))return Promise.resolve({task:resumed});
  throw Error('unexpected '+url);
};
const items=p.loadTaskItems('tg-test',{refresh:true}),results=p.loadTaskResults('tg-test');
const retry=p.retryTask('failed_only');await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(p.labeler.taskId,'tg-test');assert.equal(p.labeler.items.length,2);
assert.equal(p.labeler.items[1].state,'queued');
assert.equal(p.labeler.annotations[0].description,'一只猫坐在窗台上','重试不能清掉成功图片正文');
assert.match(JSON.stringify(resultBody.children),/一只猫坐在窗台上/);
oldItems({items:[{item_id:'i2',state:'failed',error:'过期错误'}],total:2});
oldResults({annotations:[],total:0});
assert.equal(await items,false);assert.equal(await results,false);
assert.equal(p.labeler.items[1].state,'queued');assert.equal(p.labeler.annotations.length,1);
finishBatches({tasks:[resumed]});await retry;
assert.match(elements['#labelerTaskHint'].textContent,/原批次/);
assert.equal(p.labeler.retryBusy,false);
""")

    def test_old_terminal_poll_cannot_finish_same_task_retry(self):
        self.run_case(r"""
seed();p.labeler.task={id:'tg-test',status:'done',can_retry_failed:true,progress:{done:1,total:2,failed:1}};
p.labeler.items.push({item_id:'i2',name:'failed.png',state:'failed'});
let finishOldPoll;
apiImpl=(url)=>{
  if(url.endsWith('/retry'))return Promise.resolve({task:{id:'tg-test',status:'queued',
    progress:{done:1,total:2},items:[{item_id:'i2',name:'failed.png',state:'queued'}]}});
  if(url.endsWith('/tasks'))return Promise.resolve({tasks:[]});
  if(url.endsWith('/tg-test'))return new Promise(resolve=>{finishOldPoll=resolve;});
  throw Error('旧终态不能再加载结果 '+url);
};
const old=p.pollTask();await p.retryTask('failed_only');
finishOldPoll({task:{id:'tg-test',status:'done',items:[],progress:{done:1,total:2,failed:1}}});await old;
assert.equal(p.labeler.task.status,'queued');assert.equal(p.labeler.items[1].state,'queued');
assert.equal(p.labeler.annotations.length,1);
""")


if __name__ == "__main__":
    unittest.main()
