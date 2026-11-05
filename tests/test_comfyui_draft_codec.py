"""执行真实草稿编解码器，校验落盘白名单、容量和工作流兼容性。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
import assert from 'node:assert/strict';
import { BATCH_DRAFT_KEY, BATCH_DRAFT_MAX_CHARS, encodeBatchDraft, decodeBatchDraft,
  batchDraftMappingValid } from './public/js/24b-comfyui-draft.js';
function fixture() {
  const mapping = {prompt_slot:{node_id:'1',field_name:'text'}, duration_slot:{node_id:'2',field_name:'seconds'},
    resolution_slot:null, media_ports:[{node_id:'3',field_name:'image',checked:true}]};
  const detail = {workflow_fingerprint:'fp-one',params:[
    {node_id:'1',field_name:'text',field_type:'text'}, {node_id:'2',field_name:'seconds',field_type:'number'},
    {node_id:'3',field_name:'image',field_type:'image',control:'media',media:'image'},
    {node_id:'4',field_name:'model',field_type:'text'},
  ]};
  const item = {name:'第一条',script:'原剧本',prompt:'可手改的提示词',duration:'6',resolution:'848x480',shots:2,
    files:{'0':{path:'/managed/uploads/ref.png',name:'参考.png',thumb_path:'/managed/uploads/ref-thumb.png'}}};
  const scheme = {id:'runtime-only',workflow_id:'wf-one',detail,mapping,fixed:{'4|model':'model-a'},
    collapsed:true,fixedOpen:true,items:[item],loading:true,detailRequest:{private:'temporary'}};
  return {draft:{title:'第一集',unload_after:true,schemes:[scheme]},scheme,item,detail,mapping,
    convert:{template:'h3-base',provider:'online:fixture',model:'fixture-model'}};
}
const roundtrip = (f) => decodeBatchDraft(encodeBatchDraft(f.draft,f.convert));
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行前端草稿编解码器")
class ComfyuiDraftCodecTests(unittest.TestCase):
    def run_case(self, body: str) -> None:
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-"], input=HARNESS + body,
            cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_roundtrip_keeps_inputs_and_drops_runtime_objects_and_credentials(self):
        self.run_case(r"""
const f=fixture();
f.draft.access_token='must-not-persist';f.scheme.api_key='must-not-persist';
f.item.file=new Blob(['raw media']);f.item.converting=true;f.convert.password='must-not-persist';
const raw=encodeBatchDraft(f.draft,f.convert),saved=decodeBatchDraft(raw),s=saved.draft.schemes[0];
assert.equal(BATCH_DRAFT_KEY,'cfBatchDraft:v1');assert.equal(saved.version,1);
assert.equal(saved.draft.title,'第一集');assert.equal(saved.draft.unload_after,true);
assert.equal(s.workflow_id,'wf-one');assert.equal(s.fingerprint,'fp-one');
assert.deepEqual(s.mapping,f.mapping);assert.deepEqual(s.fixed,{'4|model':'model-a'});
assert.deepEqual(s.items[0].files,f.item.files);assert.equal(s.items[0].script,'原剧本');
assert.equal(s.items[0].prompt,'可手改的提示词');assert.equal(s.items[0].shots,2);
assert.deepEqual(saved.convert,{template:'h3-base',provider:'online:fixture',model:'fixture-model'});
assert.equal(s.collapsed,true);assert.equal(s.fixedOpen,true);
for(const unexpected of ['must-not-persist','detailRequest','runtime-only','converting','file']) {
  assert.equal(raw.includes('"'+unexpected+'"'),false,unexpected);
}
assert.equal(raw.includes('must-not-persist'),false);assert.equal('detail' in s,false);assert.equal('loading' in s,false);
""")

    def test_pending_restoration_keeps_original_mapping_and_latest_text_without_busy_state(self):
        self.run_case(r"""
const f=fixture();f.scheme.restoreDraft={fingerprint:'fp-one',mapping:f.mapping,fixed:{'4|model':'restored-model'},needs_review:true};
f.scheme.detail=null;f.scheme.mapping=null;f.scheme.fixed={};f.item.prompt='加载时手改';
f.scheme.loading=true;f.scheme.checkingFiles=true;
const s=roundtrip(f).draft.schemes[0];
assert.deepEqual(s.mapping,f.mapping);assert.equal(s.fingerprint,'fp-one');
assert.equal(s.fixed['4|model'],'restored-model');assert.equal(s.items[0].prompt,'加载时手改');
assert.equal(s.needs_review,true);assert.equal('restoreDraft' in s,false);
assert.equal('checkingFiles' in s,false);assert.equal('loading' in s,false);
""")

    def test_invalid_json_version_and_shapes_are_rejected(self):
        self.run_case(r"""
for(const raw of ['{bad','null','[]','{}',JSON.stringify({version:2,draft:{},convert:{}})]) assert.throws(()=>decodeBatchDraft(raw));
for(const mutate of [
  f=>{f.draft.title=42;}, f=>{f.draft.schemes={};},f=>{f.scheme.items={};},
  f=>{f.scheme.mapping.prompt_slot={node_id:'',field_name:'text'};},
  f=>{f.scheme.fixed={'4|model':{unexpected:true}};},f=>{f.item.files=[];},
  f=>{f.item.shots=0;},f=>{f.item.shots=201;},f=>{f.item.shots='2';},
]) {const f=fixture();mutate(f);assert.throws(()=>roundtrip(f));}
""")

    def test_capacity_and_field_limits_fail_instead_of_truncating_text(self):
        self.run_case(r"""
const f=fixture();f.item.script='文'.repeat(100001);assert.throws(()=>roundtrip(f));
assert.equal(f.item.script.length,100001,'失败不能改写用户输入');
const g=fixture();g.scheme.items=Array.from({length:11},()=>({...g.item,script:'文'.repeat(100000),files:{}}));
assert.throws(()=>encodeBatchDraft(g.draft,g.convert),/保存上限/);
assert.throws(()=>decodeBatchDraft(' '.repeat(BATCH_DRAFT_MAX_CHARS+1)),/过大/);
const h=fixture();h.scheme.items=Array.from({length:201},()=>({...h.item}));assert.throws(()=>roundtrip(h));
""")

    def test_media_bytes_remote_and_external_paths_cannot_be_persisted(self):
        self.run_case(r"""
for(const path of ['data:image/png;base64,AAAA','blob:local-id','https://example.test/uploads/ref.png',
  '//example.test/uploads/ref.png','/external/ref.png','/managed/uploads/../private.png',
  'C:\\managed\\uploads\\..\\private.png']) {
  const f=fixture();f.item.files['0'].path=path;assert.throws(()=>roundtrip(f),path);
}
const f=fixture();f.item.files['0'].data='data:image/png;base64,AAAA';
f.item.files['0'].thumb_path='https://example.test/uploads/thumb.png?token=must-not-persist';
const raw=encodeBatchDraft(f.draft,f.convert),file=decodeBatchDraft(raw).draft.schemes[0].items[0].files['0'];
assert.equal(file.thumb_path,'');assert.equal(raw.includes('base64'),false);assert.equal(raw.includes('must-not-persist'),false);
const g=fixture();g.item.files['0'].path='C:\\managed\\uploads\\ref.png';assert.equal(roundtrip(g).draft.schemes[0].items[0].files['0'].path,g.item.files['0'].path);
""")

    def test_sensitive_fixed_fields_are_rejected(self):
        self.run_case(r"""
for(const field of ['api_key','api-key','access_token','token','password','secret','credential']) {
  const f=fixture();f.scheme.fixed={['4|'+field]:'must-not-persist'};
  assert.throws(()=>encodeBatchDraft(f.draft,f.convert),/口令字段/);
}
""")

    def test_mapping_requires_matching_fingerprint_and_existing_compatible_slots(self):
        self.run_case(r"""
const f=fixture(),saved=roundtrip(f).draft.schemes[0];
assert.equal(batchDraftMappingValid(saved,f.detail),true);
assert.equal(batchDraftMappingValid(saved,{...f.detail,workflow_fingerprint:'fp-two'}),false);
assert.equal(batchDraftMappingValid({...saved,fingerprint:''},f.detail),false);
assert.equal(batchDraftMappingValid(saved,{...f.detail,params:f.detail.params.filter(s=>s.node_id!=='1')}),false);
assert.equal(batchDraftMappingValid(saved,{...f.detail,params:f.detail.params.map(s=>s.node_id==='3'?{...s,media:'text'}:s)}),false);
assert.equal(batchDraftMappingValid(saved,{...f.detail,params:f.detail.params.filter(s=>s.node_id!=='4')}),false);
assert.equal(batchDraftMappingValid(saved,{...f.detail,params:f.detail.params.map(s=>s.node_id==='4'?{...s,control:'media'}:s)}),false);
assert.equal(batchDraftMappingValid({mapping:null,fingerprint:'',fixed:{}},f.detail),true);
assert.equal(batchDraftMappingValid({mapping:null,fingerprint:'fp-one',fixed:{}},f.detail),false);
""")


if __name__ == "__main__":
    unittest.main()
