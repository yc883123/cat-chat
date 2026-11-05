"""执行真实批量函数，检查草稿保存、恢复与迟到回包的归属。"""
from __future__ import annotations

import shutil
import subprocess
import unittest

from tests.test_comfyui_async_state import HARNESS, ROOT


SETUP = r"""
const storage = new Map();
context.localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
context.state.bootstrap.data_location={data_dir:'/managed'};
function savedFixture(){
  const f=fixture();
  f.scheme.detail.workflow_fingerprint='fingerprint1';
  f.scheme.detail.params.forEach(slot=>{slot.field_type='image';slot.control='media';});
  f.item.files={'0':{path:'/managed/uploads/a.png',name:'a.png',thumb_path:''}};
  f.item.prompt='保留的提示词';p.batch.draft.title='恢复批次';p.batch.draft.unload_after=true;
  const detail=JSON.parse(JSON.stringify(f.scheme.detail));
  const raw=context.encodeBatchDraft(p.batch.draft,p.batch.convert);
  return {...f,detail,raw};
}
function reload(raw){
  if(raw)storage.set(context.BATCH_DRAFT_KEY,raw);
  p.batch.draft=null;p.batch.draftLoaded=false;p.batch.draftSavedText='';p.batch.draftStatus='';p.batch.draftError='';
  p.ensureBatchDraft();
}
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行前端逻辑")
class ComfyuiDraftStateTests(unittest.TestCase):
    def run_case(self, body: str):
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-"], input=HARNESS + SETUP + body,
            cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_restore_loads_authoritative_workflow_and_checks_uploaded_files(self):
        self.run_case(r"""
const f=savedFixture(),calls=[];
context.api=async(path,options)=>{calls.push({path,body:options?.body});return path==='/api/uploads/check'?{missing:[]}:{workflow:f.detail};};
reload(f.raw);await flush();
const s=p.batch.draft.schemes[0];
assert.equal(p.batch.draft.title,'恢复批次');assert.equal(p.batch.draft.unload_after,true);
assert.equal(s.items[0].prompt,'保留的提示词');assert.equal(s.items[0].files['0'].path,'/managed/uploads/a.png');
assert.notEqual(s.detail,null);assert.equal(s.loading,false);assert.equal(s.filesUnverified,false);
assert.equal(s.restoreDraft,null);assert.equal(s.restoreNeedsReview,false);
assert.deepEqual(calls.map(row=>row.path),['/api/comfyui/workflows/workflow1','/api/uploads/check']);
assert.deepEqual(Array.from(calls[1].body.paths),['/managed/uploads/a.png']);
""")

    def test_changed_workflow_preserves_text_and_blocks_submit_until_review(self):
        self.run_case(r"""
const f=savedFixture();let submitted=0;f.detail.workflow_fingerprint='fingerprint2';
context.api=async(path)=>{if(path==='/api/comfyui/batches'){submitted++;return {batch:{id:'new'}};}return {workflow:f.detail};};
reload(f.raw);await flush();const s=p.batch.draft.schemes[0];
assert.equal(s.restoreNeedsReview,true);assert.deepEqual(Object.keys(s.items[0].files),[]);
assert.equal(s.items[0].script,'旧剧本');assert.equal(s.items[0].prompt,'保留的提示词');
await p.submitBatch();assert.equal(submitted,0);assert.match(messages.at(-1),/先完成草稿/);
const target={dataset:{batchAction:'confirm-restored-workflow',scheme:s.id},closest:()=>target};
p.onBatchClick({target,preventDefault(){}});assert.equal(s.restoreNeedsReview,false);
open=false;await p.submitBatch();assert.equal(submitted,1);
""")

    def test_clear_invalidates_late_detail_and_conversion_and_does_not_revive_storage(self):
        self.run_case(r"""
const f=savedFixture(),later=defer();context.api=()=>later.promise;
reload(f.raw);const old=p.batch.draft.schemes[0];
p.scheduleBatchDraftSave();p.clearBatchDraft();p.flushBatchDraft();
later.resolve({workflow:f.detail});await flush();
assert.equal(old.detail,null);assert.equal(storage.has(context.BATCH_DRAFT_KEY),false);
assert.equal(p.batch.draft.title,'');assert.equal(p.batch.draft.schemes[0].items.length,0);
reload();assert.equal(p.batch.draft.schemes[0].items.length,0);
const second=savedFixture(),done=defer();p.batch.draftLoaded=true;second.item.prompt='';
context.api=async()=>({task:{id:'old-convert'}});p.setWait(()=>done.promise);
const pending=p.batchConvert(second.scheme,0);await flush();p.clearBatchDraft();done.resolve('迟到结果');
assert.equal(await pending,false);assert.notEqual(second.item.prompt,'迟到结果');
p.flushBatchDraft();assert.equal(storage.has(context.BATCH_DRAFT_KEY),false);
""")

    def test_missing_foreign_or_unverifiable_files_never_restore_as_ready(self):
        self.run_case(r"""
const f=savedFixture();let checks=0;
context.api=async(path)=>{if(path==='/api/uploads/check'){checks++;return {missing:['/managed/uploads/a.png']};}return {workflow:f.detail};};
reload(f.raw);await flush();let s=p.batch.draft.schemes[0];
assert.deepEqual(Object.keys(s.items[0].files),[]);assert.match(s.restoreNotice,/失效/);
context.state.bootstrap.data_location.data_dir='/other-instance';checks=0;
reload(f.raw);await flush();s=p.batch.draft.schemes[0];
assert.deepEqual(Object.keys(s.items[0].files),[]);assert.equal(checks,0,'不把外部目录交给只检查当前缓存树的接口');
context.state.bootstrap.data_location.data_dir='/managed';
context.api=async(path)=>{if(path==='/api/uploads/check')throw new Error('断线');return {workflow:f.detail};};
reload(f.raw);await flush();s=p.batch.draft.schemes[0];assert.equal(s.filesUnverified,true);
await p.submitBatch();assert.match(messages.at(-1),/先完成草稿/);
context.api=async()=>({missing:[]});await p.checkBatchRestoredFiles(s);assert.equal(s.filesUnverified,false);
""")

    def test_input_debounce_pagehide_flush_and_storage_failure_keep_live_draft(self):
        self.run_case(r"""
const f=savedFixture();p.batch.draftLoaded=true;p.batch.draftSavedText='';
const target={dataset:{scheme:f.scheme.id,item:'0',batchItem:'script'},value:'用户新写的剧本',matches:()=>false,closest:()=>null};
p.onBatchInput({target});const timerId=p.batch.draftSaveTimer;
target.value='最后一次输入';p.onBatchInput({target});assert.notEqual(p.batch.draftSaveTimer,timerId);
assert.equal(timers.has(timerId),false,'防抖取消旧保存');p.flushBatchDraft();
assert.equal(context.decodeBatchDraft(storage.get(context.BATCH_DRAFT_KEY)).draft.schemes[0].items[0].script,'最后一次输入');
context.localStorage.setItem=()=>{const error=new Error('Quota');error.name='QuotaExceededError';throw error;};
f.item.script='空间不足时仍留在界面';p.flushBatchDraft();
assert.match(p.batch.draftStatus,/草稿未保存/);assert.equal(f.item.script,'空间不足时仍留在界面');
const count=messages.length;p.flushBatchDraft();assert.equal(messages.length,count,'同一错误不反复刷提示');
context.localStorage.setItem=(key,value)=>storage.set(key,value);p.flushBatchDraft();assert.equal(p.batch.draftStatus,'草稿已保存到本机');
""")

    def test_fixed_parameter_input_is_recorded_and_corrupt_or_unknown_storage_does_not_break_entry(self):
        self.run_case(r"""
const f=savedFixture();p.batch.draftLoaded=true;
const target={dataset:{scheme:f.scheme.id,batchFixed:'7|steps'},value:'24',matches:selector=>selector==='[data-batch-fixed]'};
p.onBatchInput({target});assert.equal(f.scheme.fixed['7|steps'],'24');
for(const raw of ['{bad','{"version":42}','{"version":1,"draft":null}']){
  reload(raw);assert.equal(p.batch.draft.schemes.length,1);assert.equal(p.batch.draft.schemes[0].items.length,0);
  assert.match(p.batch.draftStatus,/草稿未恢复/);
}
""")

    def test_copy_keeps_review_and_file_restrictions_and_restarts_pending_detail(self):
        self.run_case(r"""
const f=savedFixture();f.scheme.restoreNeedsReview=true;
const reviewed=p.addBatchScheme(f.scheme);assert.equal(reviewed.restoreNeedsReview,true);
f.scheme.restoreNeedsReview=false;f.scheme.filesUnverified=true;
context.api=async()=>{throw new Error('仍断线');};
const unchecked=p.addBatchScheme(f.scheme);await flush();assert.equal(unchecked.filesUnverified,true);
const later=defer();context.api=()=>later.promise;
f.scheme.detail=null;f.scheme.loading=true;
const pending=p.addBatchScheme(f.scheme);assert.equal(pending.loading,true);
later.resolve({workflow:f.detail});await flush();assert.notEqual(pending.detail,null);assert.equal(pending.loading,false);
const saved=context.decodeBatchDraft(f.raw).draft.schemes[0];
f.scheme.restoreDraft=saved;f.scheme.restoreNeedsReview=true;f.scheme.detail=null;
context.api=async(path)=>path==='/api/uploads/check'?{missing:[]}:{workflow:{...f.detail,workflow_fingerprint:'changed'}};
const restoring=p.addBatchScheme(f.scheme);await flush();assert.equal(restoring.restoreNeedsReview,true);
assert.equal(restoring.items[0].prompt,'保留的提示词');
""")

    def test_restoring_many_schemes_does_not_rely_on_clock_or_random_for_identity(self):
        self.run_case(r"""
const f=savedFixture(),saved=context.decodeBatchDraft(f.raw);
saved.draft.schemes=Array.from({length:50},()=>JSON.parse(JSON.stringify(saved.draft.schemes[0])));
const raw=context.encodeBatchDraft(saved.draft,saved.convert);
vm.runInContext('Date.now=()=>123456;Math.random=()=>0;',context);
const later=defer();let loads=0;context.api=()=>{loads++;return later.promise;};
reload(raw);const ids=p.batch.draft.schemes.map(scheme=>scheme.id);
assert.equal(ids.length,50);assert.equal(new Set(ids).size,50);assert.equal(loads,50);
later.resolve({workflow:f.detail});await flush();
assert.ok(p.batch.draft.schemes.every(scheme=>scheme.detail!==null&&scheme.loading===false));
""")
