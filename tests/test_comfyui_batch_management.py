"""执行真实批次管理函数，守住删除范围、部分失败与历史编辑竞态。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
import json
from html.parser import HTMLParser

from tests.test_comfyui_async_state import HARNESS, ROOT


SETUP = r"""
const confirms=[];
context.window={confirm:message=>{confirms.push(message);return true;}};
context.mediaKind=(path,name)=>/\.mp4$/.test(name||path)?'video':'image';
vm.runInContext(`
  startBatchPolling=()=>{};stopBatchPolling=()=>{};setComfyuiPane=()=>{};setComfyuiResultsSegment=()=>{};
  globalThis.management={batchRunMarkup,batchRunCardMarkup,syncBatchSelection,
    deleteBatchTaskIds,deleteBatchItem,deleteSelectedBatchItems,deleteCurrentBatch,editCurrentBatch};
`,context);
const m=context.management;
function runFixture(count=2){
  const items=Array.from({length:count},(_,index)=>({task_id:'task-'+index,name:'条目'+index,status:index?'completed':'running',outputs:['one.png'],done:0,total:1}));
  p.batch.runId='batch-a';p.batch.runData={id:'batch-a',title:'测试批次',items};p.batch.view='run';
  p.batch.selectedItems.clear();p.batch.deletingItems.clear();p.batch.deletingBatches.clear();
  return items;
}
const wf=id=>({id,params:[{node_id:'1',field_name:'text',field_type:'text',control:'multiline'}],prompt:{'1':{inputs:{text:''}}}});
const snapshot=id=>({workflow_id:'wf'+id,mapping:{prompt_slot:{node_id:'1',field_name:'text'},media_ports:[]},items:[{name:id,prompt:id+' original',shots:1,media_inputs:[]}]});
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行真实前端管理函数")
class ComfyuiBatchManagementTests(unittest.TestCase):
    def run_case(self, body: str):
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-"],
                                input=HARNESS + SETUP + body, cwd=ROOT, text=True,
                                encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_controls_are_visible_for_running_and_completed_and_selection_is_scoped(self):
        self.run_case(r"""
const rows=runFixture();const markup=m.batchRunMarkup();
assert.ok(markup.includes('data-batch-selection-toolbar'));assert.ok(markup.includes('删除整个批次'));
for(const row of rows){const card=m.batchRunCardMarkup(row);assert.ok(card.includes('cbm-delete-product danger'));assert.ok(card.includes('删除产物'));}
p.batch.selectionMode=true;p.batch.selectedItems.add('task-0');assert.ok(m.batchRunCardMarkup(rows[0]).includes('data-batch-task-check="task-0" checked'));
m.syncBatchSelection({id:'batch-b',items:[{task_id:'other',status:'completed'}]});
assert.equal(p.batch.selectedItems.size,0);assert.equal(p.batch.selectionMode,false);
""")

    def test_selected_delete_uses_exact_ids_chunks_and_explicit_confirmation(self):
        self.run_case(r"""
runFixture(250);const calls=[];
context.api=async(path,options)=>{calls.push({path,...options});return {deleted:options.body.task_ids.length,failed:[],skipped:[]};};
await m.deleteBatchTaskIds(p.batch.runData.items.map(row=>row.task_id));
assert.deepEqual(calls.map(row=>row.body.task_ids.length),[200,50]);
assert.equal(new Set(calls.flatMap(row=>row.body.task_ids)).size,250);
assert.ok(calls.every(row=>row.path==='/api/comfyui/tasks'&&row.method==='DELETE'&&row.body.cancel_active===true));
assert.match(confirms[0],/250/);assert.match(confirms[0],/批次内容保留/);assert.match(confirms[0],/先取消/);
assert.equal(p.batch.deletingBatches.size,0);assert.equal(p.batch.deletingItems.size,0);
""")

    def test_partial_failure_keeps_failed_selection_and_malformed_response_is_not_success(self):
        self.run_case(r"""
runFixture();p.batch.selectedItems=new Set(['task-0','task-1']);
context.api=async()=>({deleted:1,failed:[{id:'task-1',reason:'远端尚未停止'}],skipped:[]});
await m.deleteBatchTaskIds(['task-0','task-1']);
assert.equal(p.batch.selectedItems.has('task-0'),false);assert.equal(p.batch.selectedItems.has('task-1'),true);
assert.match(messages.at(-1),/已删除 1 条，1 条未删除/);
runFixture();context.api=async()=>({});await m.deleteBatchTaskIds(['task-0']);
assert.match(messages.at(-1),/删除未全部完成/);assert.match(messages.at(-1),/响应不完整/);
""")

    def test_cancelled_confirmation_does_not_delete_and_unselected_ids_are_excluded(self):
        self.run_case(r"""
runFixture();const calls=[];
context.api=async(path,options)=>{calls.push(options.body.task_ids);return {deleted:options.body.task_ids.length,failed:[],skipped:[]};};
context.window.confirm=()=>false;await m.deleteBatchTaskIds(['task-0']);assert.equal(calls.length,0);
context.window.confirm=()=>true;await m.deleteBatchTaskIds(['task-1','foreign-task']);
assert.equal(JSON.stringify(calls),JSON.stringify([['task-1']]));
""")

    def test_preview_placeholder_attributes_survive_real_html_parsing(self):
        markup = self.run_case(r"""
runFixture();console.log(m.batchRunCardMarkup({task_id:'quoted-task',name:'条目',status:'completed',outputs:['scene "1" & <x>.png']}));
""")

        class PreviewParser(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.preview = None

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if attributes.get("data-batch-preview-task") == "quoted-task":
                    self.preview = attributes

        parser = PreviewParser()
        parser.feed(markup)
        self.assertIsNotNone(parser.preview)
        self.assertEqual(json.loads(parser.preview["data-comfyui-preview-key"]), ["quoted-task", 'scene "1" & <x>.png'])
        self.assertEqual(parser.preview["data-comfyui-preview-name"], 'scene "1" & <x>.png')
        self.assertEqual(parser.preview["data-batch-preview-name"], 'scene "1" & <x>.png')
        self.assertIn("scene%20%221%22%20%26%20%3Cx%3E.png", parser.preview["data-comfyui-preview-url"])

    def test_delete_cannot_repeat_or_reset_a_different_batch(self):
        self.run_case(r"""
runFixture();const later=defer();let calls=0;
context.api=()=>{calls++;return later.promise;};
const first=m.deleteBatchItem('task-0');await m.deleteBatchItem('task-0');assert.equal(calls,1);
const other={id:'batch-b',items:[{task_id:'task-b',status:'running'}]};
p.batch.runId='batch-b';p.batch.runData=other;
later.resolve({deleted:1,failed:[],skipped:[]});await first;
assert.equal(p.batch.runId,'batch-b');assert.equal(p.batch.runData,other);assert.equal(p.batch.view,'run');
runFixture();const whole=defer();context.api=()=>whole.promise;const pending=m.deleteCurrentBatch();
p.batch.runId='batch-b';p.batch.runData=other;
whole.resolve({ok:true,deleted:2,failed:[],skipped:[]});await pending;
assert.equal(p.batch.runId,'batch-b');assert.equal(p.batch.runData,other);assert.equal(p.batch.view,'run');
""")

    def test_latest_edit_wins_and_late_edit_never_overwrites_manual_input(self):
        self.run_case(r"""
p.batch.draft=null;p.batch.runId='';p.batch.view='run';const slow=defer();
context.api=async(path)=>path.includes('/batches/')
 ? {batch:{id:path.includes('/A?')?'A':'B',schemes:[snapshot(path.includes('/A?')?'A':'B')],items:[]}}
 : path.endsWith('wfA')?slow.promise:{workflow:wf('wfB')};
const older=m.editCurrentBatch('A');await flush();await m.editCurrentBatch('B');
p.batch.draft.schemes[0].items[0].prompt='人工新稿';slow.resolve({workflow:wf('wfA')});await older;
assert.equal(p.batch.draftSourceId,'B');assert.equal(p.batch.draft.schemes[0].items[0].prompt,'人工新稿');
const delayed=defer();context.api=async(path)=>path.includes('/batches/')?{batch:{id:'A',schemes:[snapshot('A')],items:[]}}:delayed.promise;
const pending=m.editCurrentBatch('A');await flush();p.batch.draft.schemes[0].items[0].prompt='等待期间手改';
delayed.resolve({workflow:wf('wfA')});await pending;
assert.equal(p.batch.draftSourceId,'B');assert.equal(p.batch.draft.schemes[0].items[0].prompt,'等待期间手改');
assert.match(messages.at(-1),/未覆盖/);
""")

    def test_edit_protects_unblurred_form_value_and_navigation(self):
        self.run_case(r"""
runFixture();p.batch.draft={title:'旧标题',schemes:[]};let title='旧标题';
context.document.querySelectorAll=()=>[{dataset:{batchField:'title'},value:title,checked:false}];
const slow=defer();context.api=async(path)=>path.includes('/batches/')?{batch:{id:'source',schemes:[snapshot('source')],items:[]}}:slow.promise;
const pending=m.editCurrentBatch('source');await flush();title='尚未失焦的人工标题';
slow.resolve({workflow:wf('source')});await pending;
assert.equal(p.batch.draft.title,'旧标题');assert.equal(p.batch.runId,'batch-a');assert.match(messages.at(-1),/未覆盖/);
const later=defer();context.api=()=>later.promise;const request=m.editCurrentBatch('source');
p.batch.runId='batch-b';later.resolve({batch:{id:'source',schemes:[],items:[]}});await request;
assert.equal(p.batch.runId,'batch-b');assert.equal(p.batch.draft.title,'旧标题');
""")


if __name__ == "__main__":
    unittest.main()
