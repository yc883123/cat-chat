"""Recipe frontend contracts and executable DOM-free logic; no browser launches."""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RecipeFrontendAssets(unittest.TestCase):
    def test_composition_root_owns_cross_domain_handoff(self):
        comfy = (ROOT / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        bind = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        self.assertNotIn("superAgentView", comfy)
        self.assertNotIn("/api/super-agent", comfy)
        self.assertIn("setWorkflowIntentHandler", comfy)
        self.assertIn("workflowIntentHandler(JSON.parse(JSON.stringify", comfy)
        self.assertIn("setWorkflowIntentHandler((intent)", bind)
        self.assertIn("openRecipeWorkflowIntent(intent)", bind)

    def test_markup_ids_unique_and_mobile_media_controls_scoped(self):
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))
        for name in ("superAgentRecipeDialog", "recipeReviewDialog", "recipeCustomForm", "recipeImages", "recipeFolder", "recipeDuration", "recipeSharedMedia"):
            self.assertIn(name, ids)
        self.assertIn('webkitdirectory', html)
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        self.assertIn(".recipe-check input[type=checkbox]", css)
        self.assertIn(".super-agent-recipe-dialog", css)
        self.assertIn("max-width: 100vw; height: 100dvh", css)

    def test_polling_preserves_single_card_editors_and_recipe_projection(self):
        source = (ROOT / "public/js/28-super-agent.js").read_text(encoding="utf-8")
        self.assertIn('editorWasEmpty || (!draft.dirty && !editors.contains(document.activeElement))', source)
        self.assertIn('data-recipe-results-id=', source)
        self.assertIn('resultHost.innerHTML =', source)
        self.assertNotIn('return `<article class="recipe-item"><h3>${text(item.source_path)}</h3><p>反推', source)
        self.assertIn('const conflict = Object.entries(draft.items)', source)
        self.assertIn('仅统计纳入清单且产物完整的项目', source)
        self.assertIn('核对素材并确认反推', source)
        self.assertIn('已明确排除：', source)
        self.assertIn('recipeThread ? recipeState.batches.get(threadId) : null', source)

    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_recipe_state_actions_review_and_media_execute(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/28-super-agent.js','utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
const nodes=new Map();
const $=key=>{if(!nodes.has(key))nodes.set(key,{value:'',disabled:false,textContent:'',innerHTML:'',open:false,dataset:{},showModal(){this.open=true;},close(){this.open=false;},querySelectorAll(){return [];}});return nodes.get(key);};
const requests=[];
const context={$,assert,console,process,globalThis,setTimeout,clearTimeout,state:{providerModelCatalogs:{}},
 escapeHtml:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
 localFileUrl:path=>'/api/file?token=auth&path='+encodeURIComponent(path),window:{confirm:()=>true},
 api:async(path,options)=>{requests.push({path,body:JSON.parse(JSON.stringify(options.body))});return {batch:{...context.nextBatch}};}};
vm.runInNewContext(source+`
(async()=>{
const batch={id:'batch',thread_id:'thread',revision:1,revision_hash:'hash-1',status:'review',capabilities:['edit','confirm_generate','retry_collect'],model:{model_key:'supplier',model_name:'vision'},target:{url:'local-target',fingerprint:'target-hash'},recipe:{name:'Recipe',workflow_id:'workflow',workflow_fingerprint:'workflow-hash',prompt_slot:{node_id:'7',field_name:'prompt'},media_slots:[{role:'picture_1',node_id:'8',field_name:'image'}],workflow:{'7':{inputs:{prompt:'fixed'}}}},template:{id:'template',body:'EXACT TEMPLATE',result_rule:{type:'text'}},duration_seconds:15,shots:2,overrides:[],items:[{id:'one',source_path:'one.png',included:true,requirements:'old',prompt:'old prompt',media:[{role:'picture_1',path:'managed/image.png',name:'image.png'}],reverse:{status:'done'},generate:{status:'failed'},effective_params:[{node_id:'7',field_name:'prompt',value:'new prompt'}],outputs:[]},{id:'two',source_path:'excluded.png',included:false,prompt:'excluded',generate:{status:'pending'}}]};
batch.items[0].effective_workflow={id:'effective-one-ref',fingerprint:'effective-one-hash',prompt:{'7':{inputs:{prompt:'saved-source'}},'8':{inputs:{image:'one.png'}}}};
batch.items[0].effective_media_inputs=[{node_id:'8',field_name:'image',path:'managed/image.png'}];
batch.recipe.workflow.unused_reference_6={inputs:{image:'UNUSED_SIXTH_REFERENCE'}};
batch.recipe.media_slots.push({role:'picture_6',node_id:'unused_reference_6',field_name:'image'});
const markup=recipeReviewMarkup(batch,'confirm_generate');
assert.match(markup,/模板来源指纹/);assert.match(markup,/effective-one-ref/);assert.match(markup,/effective-one-hash/);
assert.ok(!markup.includes('UNUSED_SIXTH_REFERENCE'));assert.ok(!markup.includes('unused_reference_6'));
assert.ok(!markup.includes('picture_6'));assert.ok(markup.includes('本项实际图片节点映射（1 张）'));
assert.equal(markup.split('new prompt').length-1,0); // Prompt override is omitted from the separate effective parameter block.
assert.doesNotThrow(()=>assertRecipeReviewSnapshot(batch,'confirm_generate'));
const missingEffective={...batch,items:[{...batch.items[0],effective_workflow:null}]};
assert.throws(()=>assertRecipeReviewSnapshot(missingEffective,'confirm_generate'),/缺少服务器最终生效工作流快照/);
assert.doesNotThrow(()=>assertRecipeReviewSnapshot(missingEffective,'confirm_reverse'));
for(const value of ['EXACT TEMPLATE','supplier','vision','workflow-hash','target-hash','one.png','excluded.png','15','2'])assert.ok(markup.includes(value),value);
assert.equal(recipeCan(batch,'confirm_generate'),true);
assert.equal(recipeCan(batch,'pause'),false);
assert.equal(recipeRequest('same',{revision:1}),recipeRequest('same',{revision:1}));
assert.notEqual(recipeRequest('same',{revision:1}),recipeRequest('same',{revision:2}));
const image=recipeOutputMarkup({path:'managed/out.png',name:'<unsafe>',type:'image'});
assert.ok(image.includes('/api/file?token=auth'));assert.ok(image.includes('loading="lazy"'));assert.ok(!image.includes('_thumb'));assert.ok(!image.includes('<unsafe>'));
assert.ok(recipeOutputMarkup({path:'managed/out.mp4',type:'video'}).includes('preload="none"'));
assert.ok(recipeOutputMarkup({path:'managed/out.mp4',name:'C:'+String.fromCharCode(92)+'outputs'+String.fromCharCode(92)+'clip.mp4',type:'video'}).includes('打开 / 保存 clip.mp4'));
assert.ok(!recipeOutputMarkup({path:'managed/unknown.bin',type:'file'}).includes('<img'));
const draft=recipeDraft(batch);draft.dirty=true;draft.revision=1;draft.items={one:{prompt:'edited prompt',included:false}};draft.baseline={one:{prompt:'old prompt',included:true}};
context.nextBatch={...batch,revision:2,revision_hash:'hash-2',items:[{...batch.items[0],prompt:'edited prompt',included:false},batch.items[1]]};
viewState.selectedId='thread';recipeState.batches.set('thread',batch);renderRecipeBatch=()=>{};
await recipeAction('confirm_generate');
assert.equal(requests.length,1);assert.equal(requests[0].body.action,'edit');assert.equal(requests[0].body.items.one.prompt,'edited prompt');assert.equal(requests[0].body.items.one.included,false);assert.equal(requests[0].body.revision,1);
assert.equal(recipeState.review.batch.revision,2);assert.equal($('#recipeReviewDialog').open,true);
context.nextBatch={...context.nextBatch,status:'generation_running',revision:3};await confirmRecipeReview();
assert.equal(requests.length,2);assert.equal(requests[1].body.action,'confirm_generate');assert.equal(requests[1].body.revision,2);assert.equal(requests[1].body.revision_hash,'hash-2');
const stale=recipeDraft({...batch,thread_id:'stale'});stale.dirty=true;stale.revision=1;stale.items={one:{prompt:'mine'}};stale.baseline={one:{prompt:'old'}};
await assert.rejects(()=>saveRecipeDraft({...batch,thread_id:'stale',revision:5}),/服务器版本已变化/);
assert.equal(stale.items.one.prompt,'mine');
const numeric=recipeNumericSlots({workflow:{'1':{inputs:{seed:7,width:768,prompt:'text',value:15,image:'x'}}},prompt_slot:{node_id:'1',field_name:'prompt'},duration_slot:{node_id:'1',field_name:'value'},media_slots:[{node_id:'1',field_name:'image'}]});
assert.equal(numeric.length,2);assert.ok(numeric.some(slot=>slot.field_name==='seed'));
const fixedIntent={workflow:{prompt:{'1':{inputs:{seed:7,enabled:false,path:'saved-path',sampler_name:'euler',model:'saved-model',prompt:'saved prompt'}}},params:[{node_id:'1',field_name:'seed',field_type:'seed'},{node_id:'1',field_name:'enabled',field_type:'bool'},{node_id:'1',field_name:'path',field_type:'text'},{node_id:'1',field_name:'sampler_name',field_type:'text'},{node_id:'1',field_name:'model',field_type:'text'},{node_id:'1',field_name:'prompt',field_type:'text'}]},overrides:[{node_id:'1',field_name:'seed',value:'42'},{node_id:'1',field_name:'enabled',value:'true'},{node_id:'1',field_name:'path',value:'saved-path'},{node_id:'1',field_name:'sampler_name',value:'euler'},{node_id:'1',field_name:'model',value:'saved-model'}]};
const fixed=recipeCustomFixedOverrides(fixedIntent);
assert.equal(fixed.length,2);assert.equal(fixed[0].value,42);assert.equal(fixed[1].value,true);
for(const [field,value] of [['path','changed-path'],['sampler_name','dpmpp'],['model','changed-model']])assert.throws(()=>recipeCustomFixedOverrides({...fixedIntent,overrides:[{node_id:'1',field_name:field,value}]}),/此参数尚不支持交给总管，请恢复工作流值或保存为新工作流/);
assert.throws(()=>recipeCustomFixedOverrides({...fixedIntent,workflow:{params:fixedIntent.workflow.params},overrides:[{node_id:'1',field_name:'model',value:'saved-model'}]}),/此参数尚不支持/);
const managedIntent={...fixedIntent,overrides:[...fixedIntent.overrides,{node_id:'1',field_name:'prompt',value:'new per-item prompt'}]};
assert.throws(()=>recipeCustomFixedOverrides(managedIntent),/prompt.*此参数尚不支持/);
assert.equal(recipeCustomFixedOverrides(managedIntent,new Set(['1|prompt'])).length,2);
recipeState.intent={...fixedIntent,overrides:[{node_id:'1',field_name:'sampler_name',value:'changed'}]};
$('#recipePromptSlot').value=JSON.stringify({node_id:'1',field_name:'prompt'});$('#recipeCustomTemplate').value='template';
const requestsBefore=requests.length;await saveCustomRecipe({preventDefault(){}});
assert.equal(requests.length,requestsBefore);assert.match($('#recipeCustomError').textContent,/此参数尚不支持交给总管/);
})().catch(error=>{console.error(error);process.exitCode=1;});
`,Object.assign(context,{requests,context}));
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
