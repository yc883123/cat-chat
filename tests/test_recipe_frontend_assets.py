"""Recipe frontend contracts and executable DOM-free logic; no browser launches."""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
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
        for name in ("superAgentRecipeDialog", "recipeReviewDialog", "superAgentHandoffDialog", "recipeReviewPrecheck",
                     "recipeWorkflowSelect", "recipeCreatePrompt", "comfyuiDraftHint", "recipeDuration",
                     "comfyuiSaveAsDialog", "comfyuiSaveAsList", "comfyuiSaveAsDiscard", "comfyuiSaveAsConfirm"):
            self.assertIn(name, ids)
        # Q2（2026-11-03 拍板）：旧方案弹窗从界面去掉，交接不再先弹方案映射。
        for gone in ("superAgentRecipeCustomDialog", "recipeSourceDialog", "recipeModeCards", "recipeSelect"):
            self.assertNotIn(gone, ids)
        self.assertIn("交给任务总管 · 批量准备", html)
        self.assertIn('webkitdirectory', html)
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        self.assertIn(".recipe-check input[type=checkbox]", css)
        self.assertIn(".super-agent-recipe-dialog", css)
        self.assertIn(".comfyui-draft-hint", css)
        self.assertIn(".recipe-precheck", css)
        self.assertIn("max-width: 100vw; height: 100dvh", css)

    def test_shared_workflow_import_dialog_has_no_hidden_view_ancestor(self):
        class DialogParents(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.parents = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get("id") == "comfyuiImportDialog":
                    self.parents.append(list(self.stack))
                if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                    self.stack.append((tag, attrs.get("id")))

            def handle_endtag(self, tag):
                for index in range(len(self.stack) - 1, -1, -1):
                    if self.stack[index][0] == tag:
                        del self.stack[index:]
                        break

        parser = DialogParents()
        parser.feed((ROOT / "public/index.html").read_text(encoding="utf-8"))
        self.assertEqual(parser.parents, [[("html", None), ("body", None)]],
                         "Shared workflow import must remain outside hidden app views")

    def test_polling_preserves_single_card_editors_and_recipe_projection(self):
        source = (ROOT / "public/js/28-super-agent.js").read_text(encoding="utf-8")
        self.assertIn('editorWasEmpty || (!draft.dirty && !editors.contains(document.activeElement))', source)
        self.assertIn('data-recipe-results-id=', source)
        self.assertIn('resultHost.innerHTML =', source)
        self.assertNotIn('return `<article class="recipe-item"><h3>${text(item.source_path)}</h3><p>反推', source)
        self.assertIn('const conflict = Object.entries(draft.items)', source)
        self.assertIn('仅统计纳入清单且产物完整的项目', source)
        self.assertIn('核对素材，生成提示词', source)
        self.assertIn('已明确排除：', source)
        self.assertIn('recipeThread ? recipeState.batches.get(threadId) : null', source)

    def test_save_as_reviews_every_scanned_slot_and_never_silently_drops_changes(self):
        source = (ROOT / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        # 另存不再是只会问名字的 window.prompt：先出完整核对清单，再提交。
        self.assertNotIn("window.prompt", source)
        self.assertIn("function saveAsRows()", source)
        self.assertIn("function renderSaveAsReview()", source)
        self.assertIn("不能另存", source)
        # 不支持的改动必须显式确认放弃，不能随手丢弃。
        self.assertIn("comfyuiSaveAsDiscard", source)
        self.assertIn("请先还原它们，或勾选确认放弃", source)
        # 隐藏槽位是可核对的：能一眼看到未显示，也能加回主表单。
        self.assertIn("未在主表单显示", source)
        self.assertIn("加到主表单", source)
        for reason in ("受管媒体入口", "提示词按任务输入处理", "自由文本 / 路径 / 代码不写进副本"):
            self.assertIn(reason, source)

    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_recipe_advanced_params_share_one_model_for_display_and_submit(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/28-super-agent.js','utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
const host={querySelectorAll:()=>inputs,innerHTML:''};
const inputs=[];
const nodes=new Map();
const $=key=>{if(key==='#recipeEffectiveControls')return host;if(!nodes.has(key))nodes.set(key,{value:'',textContent:'',innerHTML:'',hidden:false,dataset:{},querySelectorAll(){return []},showModal(){},close(){}});return nodes.get(key)};
const context={$,assert,console,setTimeout,clearTimeout,URL,state:{providerModelCatalogs:{}},document:{querySelectorAll:()=>[]},inputs,
 escapeHtml:value=>String(value??''),localFileUrl:path=>path,window:{},api:async()=>({})};
vm.runInNewContext(source+`
const recipe={id:'r1',version:2,workflow_fingerprint:'fp-one',mode:'custom',
 prompt_slot:{node_id:'1',field_name:'prompt'},duration_slot:{node_id:'1',field_name:'frames'},
 media_slots:[{role:'picture_1',node_id:'1',field_name:'image'}],
 overrides:[{node_id:'2',field_name:'steps',value:12}],
 workflow:{'1':{class_type:'Prompt',inputs:{prompt:'p',frames:15,image:'x.png'}},
           '2':{class_type:'KSampler',_meta:{title:'采样'},inputs:{steps:20,seed:7,cfg:6.5,cudnn:false,sampler:'euler',positive:['1',0]}}}};
const rows=recipeParameterRows(recipe);
assert.deepEqual(rows.map(row=>row.field_name).sort(),['cfg','cudnn','seed','steps']);
assert.equal(rows.find(row=>row.field_name==='steps').recipe_value,12);
assert.equal(rows.find(row=>row.field_name==='seed').field_type,'seed');
assert.equal(rows.find(row=>row.field_name==='cudnn').field_type,'bool');
assert.equal(rows.find(row=>row.field_name==='cfg').integer,false);
assert.ok(rows.find(row=>row.field_name==='steps').label.includes('采样'));
// 只给整数槽填小数要拦下；负数种子要拦下；超精确范围要拦下。
assert.throws(()=>recipeOverrideValue({node_id:'2',field_name:'steps',field_type:'number',integer:true},'1.5'),/整数/);
assert.throws(()=>recipeOverrideValue({node_id:'2',field_name:'seed',field_type:'seed',integer:true},'-1'),/不能是负数/);
assert.throws(()=>recipeOverrideValue({node_id:'2',field_name:'steps',field_type:'number',integer:true},'9007199254740993'),/精确/);
assert.equal(recipeOverrideValue({node_id:'2',field_name:'cfg',field_type:'number',integer:false},'7.25'),7.25);
assert.equal(recipeOverrideValue({node_id:'2',field_name:'cudnn',field_type:'bool'},'false'),false);
// 草稿键必须按方案 ID + 版本 + 指纹隔离，换成同地址的方案不会串值。
const other={...recipe,workflow_fingerprint:'fp-two'};
assert.notEqual(recipeOverrideKey(recipe,'2','steps'),recipeOverrideKey(other,'2','steps'));
// 提交值 = 方案默认 + 本批次填写；空白沿用方案值，不会凭空补项。
inputs.length=0;
const pick=(node,field,value,tag='INPUT')=>{inputs.push({dataset:{recipeOverrideNode:node,recipeOverrideField:field},value,tagName:tag})};
recipeState.recipes=[recipe];recipeState.selected='r1';
pick('2','steps','');pick('2','seed','42');pick('2','cudnn','true','SELECT');
const collected=collectRecipeOverrides(recipe);
assert.equal(collected.length,3);
assert.equal(collected.find(entry=>entry.field_name==='steps').value,12);
assert.equal(collected.find(entry=>entry.field_name==='seed').value,42);
assert.equal(collected.find(entry=>entry.field_name==='cudnn').value,true);
pick('2','seed','-3');
assert.throws(()=>collectRecipeOverrides(recipe),/不能是负数/);
`,context);
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_workflow_direct_create_submits_handoff_shape_and_safe_draft(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const nodes=new Map(),storage=new Map(),requests=[];
const makeNode=()=>({value:'',disabled:false,textContent:'',innerHTML:'',options:[],hidden:false,open:false,dataset:{},querySelectorAll(){return []},classList:{toggle(){}},closest(){return {hidden:false}},showModal(){this.open=true},close(){this.open=false}});
const $=key=>{if(!nodes.has(key))nodes.set(key,makeNode());return nodes.get(key)};
const context=vm.createContext({console,setTimeout,clearTimeout,URL,localStorage:{setItem:(key,value)=>storage.set(key,value),getItem:key=>storage.get(key)},document:{querySelectorAll:()=>[],activeElement:null},window:{confirm:()=>true}});
const source=fs.readFileSync('public/js/28-super-agent.js','utf8');
const names=new Set([...source.matchAll(/^import \{([^}]+)\}/gm)].flatMap(match=>match[1].split(',').map(name=>name.trim())));
const values={$,api:async(path,options)=>{requests.push({path,body:options?.body});return path.endsWith('/recipe-batches')?{thread_id:'t1',batch:{id:'b1'}}:{}},state:{providerModelCatalogs:{}},escapeHtml:value=>String(value??'').replaceAll('<','&lt;').replaceAll('>','&gt;'),localFileUrl:path=>'/file?path='+encodeURIComponent(path)};
const stub=new vm.SyntheticModule([...names],function(){for(const name of names)this.setExport(name,values[name]||(()=>{}))},{context});
const mod=new vm.SourceTextModule(source+'\nexport {recipeState,rememberRecipeDraft,restoreRecipeDraft,recipeMediaMarkup,itemFieldEditable,submitRecipeCreate,recipeDurationSupported,recipePromptSlotCandidates,recipeDurationSlotCandidate,recipeMappingFromWorkflow};',{context});
(async()=>{await mod.link(()=>stub);await mod.evaluate();const m=mod.namespace,s=m.recipeState;
// 文本写入位候选 = 全部文本槽：正向锚点只排最前，不再只认多行框、不因歧义报错。
const slots=[{node_id:'1',field_name:'text',field_type:'text',control:'multiline',role:''},{node_id:'2',field_name:'note',field_type:'text',control:'multiline'},{node_id:'5',field_name:'value',field_type:'number',node_title:'视频时长（秒）'},{node_id:'6',field_name:'image',field_type:'image',control:'media'},{node_id:'7',field_name:'sampler_name',field_type:'text',role:'sampler'}];
assert.equal(m.recipePromptSlotCandidates(slots).length,3);
const durationSlot=m.recipeDurationSlotCandidate(slots);
assert.equal(durationSlot.node_id,'5');assert.equal(durationSlot.field_name,'value');
const anchored=[{node_id:'9',field_name:'text',field_type:'text',role:'positive_prompt'},...slots];
assert.equal(m.recipePromptSlotCandidates(anchored)[0].node_id,'9');
// 映射初稿：唯一文本槽/唯一时长槽预选（可改可清）；素材入口由用户添加。
const mapping=m.recipeMappingFromWorkflow({params:anchored});
assert.equal(mapping.prompt_slot.node_id,'9');assert.equal(mapping.prompt_slot.field_name,'text');
assert.equal(mapping.duration_slot.node_id,'5');
// 提交形态：source=workflow，不带 model_key；用户映射（文本位/素材/时长）随批次带上。
s.createWorkflow={id:'cw-1',name:'测试工作流',workflow_fingerprint:'fp-1',prompt:{'1':{inputs:{text:'默认正文'}},'5':{inputs:{value:15}}},params:slots};
s.createMapping={prompt_slot:{node_id:'1',field_name:'text'},media_slots:[{node_id:'6',field_name:'image',media:'image'}],duration_slot:{node_id:'5',field_name:'value'}};
s.createInput={inputMode:'prompt',templateId:'recipe_minimax_h3_r2v_v2'};
$('#recipeTitle').value='批次甲';$('#recipeShots').value='3';$('#recipeDuration').value='5';
$('#recipeCreatePrompt').value='  提交正文  ';
$('#superAgentRecipeDialog').open=true;
m.rememberRecipeDraft();const persisted=[...storage.values()][0];const parsed=JSON.parse(persisted);
assert.equal(parsed.version,2);assert.equal(parsed.workflowId,'cw-1');assert.equal(parsed.title,'批次甲');assert.ok(!persisted.includes('secret'));
assert.deepEqual(parsed.mapping.prompt_slot,{node_id:'1',field_name:'text'});
await m.submitRecipeCreate({preventDefault(){}});
const create=requests.find(row=>row.path==='/api/super-agent/recipe-batches');assert.ok(create);
assert.equal(create.body.source,'workflow');assert.equal(create.body.workflow_id,'cw-1');assert.equal(create.body.workflow_fingerprint,'fp-1');
assert.equal(create.body.prompt,'提交正文');assert.equal(create.body.shots,3);assert.equal(create.body.title,'批次甲');
assert.ok(!('model_key' in create.body));
assert.equal(create.body.prompt_slot.node_id,'1');assert.equal(create.body.prompt_slot.field_name,'text');
assert.equal(create.body.duration_slot.node_id,'5');assert.equal(create.body.duration_slot.field_name,'value');
assert.equal(create.body.duration_seconds,5);
assert.equal(create.body.media_slots.length,1);assert.equal(create.body.media_slots[0].node_id,'6');
// 已填正文但没有文本写入位 → 明确拒绝（值没地方去，属映射不自洽），不发请求。
s.createMapping.prompt_slot=null;
await m.submitRecipeCreate({preventDefault(){}});
assert.equal(requests.filter(row=>row.path==='/api/super-agent/recipe-batches').length,1);
assert.ok($('#recipeCreateError').textContent.includes('文本写入位'),$('#recipeCreateError').textContent);
s.createMapping.prompt_slot={node_id:'1',field_name:'text'};
// 反推模式：带 input_mode + H3 预设模板 ID。
s.createInput.inputMode='script';
await m.submitRecipeCreate({preventDefault(){}});
const create2=requests.filter(row=>row.path==='/api/super-agent/recipe-batches').at(-1);
assert.equal(create2.body.input_mode,'script');assert.equal(create2.body.template_id,'recipe_minimax_h3_r2v_v2');
// 草稿恢复：只回填 createDraft（含映射与正文来源），不复活旧版 items/pool 结构。
$('#superAgentRecipeDialog').open=true;m.rememberRecipeDraft();
s.createDraft=null;s.restored=false;m.restoreRecipeDraft();assert.equal(s.createDraft.title,'批次甲');assert.equal(s.createDraft.workflowId,'cw-1');
assert.equal(s.createDraft.mapping.prompt_slot.node_id,'1');assert.equal(s.createDraft.mapping.prompt_slot.field_name,'text');
assert.equal(s.createDraft.inputMode,'script');
const media=m.recipeMediaMarkup([{path:'clip.mp4',name:'clip.mp4',media:'video',description:'motion'},{path:'sound.wav',name:'sound.wav',media:'audio'}]);assert.ok(media.includes('<video'));assert.ok(media.includes('<audio'));assert.ok(media.includes('&lt;Video 1&gt;'));assert.ok(media.includes('&lt;Audio 1&gt;'));
assert.equal(m.itemFieldEditable({capabilities:['edit']},{generate:{status:'failed',attempts:[{}]},edit_capabilities:{prompt:false,included:true}},'prompt'),false);
assert.equal(m.recipeDurationSupported({}),false);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(["node", "--experimental-vm-modules", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_direct_prompt_phase_and_next_step_match_generation_action(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/28-super-agent.js','utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
const nodes=new Map(),requests=[];
const makeNode=()=>({value:'',textContent:'',innerHTML:'',hidden:false,dataset:{},children:[],classList:{remove(){},add(){}},querySelectorAll(){return []},contains(){return false}});
const $=key=>{if(!nodes.has(key))nodes.set(key,makeNode());return nodes.get(key)};
const parts=new Map();const host=$('#superAgentRecipeBatch');host.dataset.batchId='phase-test';host.querySelector=key=>{if(!parts.has(key))parts.set(key,makeNode());return parts.get(key)};
const context={assert,console,setTimeout,clearTimeout,$,document:{activeElement:null},api:()=>requests.push('unexpected'),escapeHtml:value=>String(value??''),localFileUrl:path=>path,state:{}};
vm.runInNewContext(source+`
const item={id:'one',included:true,prompt:'Complete prompt',reverse:{status:'provided'},generate:{status:'pending'},media:[],outputs:[]};
const batch={id:'phase-test',thread_id:'phase-thread',revision:1,status:'draft',input_mode:'prompt',shots:2,capabilities:['confirm_generate','edit'],recipe:{name:'Prompt recipe'},items:[item,{...item,id:'two'},{...item,id:'excluded',included:false,prompt:''}]};
const original=JSON.stringify(batch),primary=recipePrimaryAction(batch);
assert.equal(primary.action,'confirm_generate');assert.equal(primary.label,'确认生成 4 条视频');
assert.equal(recipePhaseLabel(batch),'待确认生成视频');assert.equal(recipeNextCopy(batch).title,primary.label);
assert.ok(!recipeNextCopy(batch).hint.includes('向提词模型发送请求'));
renderRecipeBatch(batch);
assert.equal($('#superAgentCurrentStage').textContent,'待确认生成视频');
assert.ok($('#superAgentNextStep').innerHTML.includes(primary.label));
assert.ok(!$('#superAgentNodeList').innerHTML.includes('待生成提示词'));
assert.ok(host.querySelector('.recipe-batch-status').innerHTML.includes('待确认生成视频'));
assert.ok(!host.querySelector('.recipe-batch-editors').innerHTML.includes('修改后需重新生成提示词'));
assert.equal(recipePhaseLabel({...batch,items:[{...item,prompt:'   '}]}),'待补齐提示词');
assert.equal(recipeNextCopy({...batch,items:[{...item,prompt:''}]}).title,'待补齐提示词');
assert.equal(recipePhaseLabel({...batch,items:[]}),'待纳入项目');
assert.equal(recipeNextCopy({...batch,items:[{...item,included:false}]}).title,'待纳入项目');
assert.equal(recipeNextCopy({...batch,capabilities:[]}).title,'提示词已就绪，待核对');
const scriptBatch={...batch,input_mode:'script',items:[{...item,prompt:'',reverse:{status:'pending'}}],capabilities:['confirm_reverse']};
assert.equal(recipePhaseLabel(scriptBatch),'待生成提示词');assert.ok(recipeNextCopy(scriptBatch).hint.includes('向提词模型发送请求'));
for(const status of ['paused','generation_running','completed','cancelled','needs_review'])assert.equal(recipePhaseLabel({...batch,status}),RECIPE_PHASES[status]);
assert.equal(JSON.stringify(batch),original);assert.equal(JSON.stringify(recipePrimaryAction(batch)),JSON.stringify(primary));assert.equal(requests.length,0);
`,Object.assign(context,{requests,host}));
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_recipe_state_actions_review_and_media_execute(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/28-super-agent.js','utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
const nodes=new Map();
const $=key=>{if(!nodes.has(key))nodes.set(key,{value:'',disabled:false,textContent:'',innerHTML:'',open:false,dataset:{},showModal(){this.open=true;},close(){this.open=false;},querySelectorAll(){return [];}});return nodes.get(key);};
const requests=[];
const context={$,assert,console,process,globalThis,setTimeout,clearTimeout,URL,state:{providerModelCatalogs:{}},
 escapeHtml:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
 localFileUrl:path=>'/api/file?token=auth&path='+encodeURIComponent(path),window:{confirm:()=>true},document:{querySelectorAll:()=>[]},
 api:async(path,options)=>{requests.push({path,body:JSON.parse(JSON.stringify(options.body))});
  if(path.endsWith('/recipe/precheck'))return {checked_at:1,ok:true,target:{url:'http://127.0.0.1:8188',ok:true},checks:[]};
  if(path.endsWith('/recipe-batches'))return {thread_id:'t9',batch:{...context.nextBatch}};
  return {batch:{...context.nextBatch}};}};
vm.runInNewContext(source+`
(async()=>{
const batch={id:'batch',thread_id:'thread',revision:1,revision_hash:'hash-1',status:'review',capabilities:['edit','confirm_generate','retry_collect'],model:{model_key:'supplier',model_name:'vision'},target:{url:'http://127.0.0.1:8188',fingerprint:'target-hash'},recipe:{name:'Recipe',workflow_id:'workflow',workflow_fingerprint:'workflow-hash',prompt_slot:{node_id:'7',field_name:'prompt'},media_slots:[{role:'picture_1',node_id:'8',field_name:'image'}],workflow:{'7':{inputs:{prompt:'fixed'}}}},template:{id:'template',body:'EXACT TEMPLATE',result_rule:{type:'text'}},duration_seconds:15,shots:2,overrides:[],items:[{id:'one',source_path:'one.png',included:true,requirements:'old',prompt:'old prompt',media:[{role:'picture_1',path:'managed/image.png',name:'image.png'}],reverse:{status:'done'},generate:{status:'failed'},effective_params:[{node_id:'7',field_name:'prompt',value:'new prompt'}],outputs:[]},{id:'two',source_path:'excluded.png',included:false,prompt:'excluded',generate:{status:'pending'}}]};
batch.recipe.duration_slot={node_id:'7',field_name:'duration'};
batch.items[0].effective_workflow={id:'effective-one-ref',fingerprint:'effective-one-hash',prompt:{'7':{inputs:{prompt:'saved-source'}},'8':{inputs:{image:'one.png'}}}};
batch.items[0].effective_media_inputs=[{node_id:'8',field_name:'image',path:'managed/image.png'}];
batch.recipe.workflow.unused_reference_6={inputs:{image:'UNUSED_SIXTH_REFERENCE'}};
batch.recipe.media_slots.push({role:'picture_6',node_id:'unused_reference_6',field_name:'image'});
const markup=recipeReviewMarkup(batch,'confirm_generate');
// 审核确认页是普通用户视角：素材/提示词/数量/费用；不得出现指纹、原始 JSON、绝对路径、模板正文与内部标识。
assert.ok(!/指纹|snapshot|<pre|EXACT TEMPLATE|supplier|vision|workflow-hash|target-hash|node_id|field_name|effective-one/.test(markup), 'review markup leaked technical details');
assert.ok(!markup.includes('managed/'));assert.ok(!markup.includes('UNUSED_SIXTH_REFERENCE'));assert.ok(!markup.includes('unused_reference_6'));
assert.ok(!markup.includes('picture_6'));assert.ok(!markup.includes('new prompt'));
assert.ok(markup.includes('old prompt'));assert.ok(markup.includes('excluded.png'));
assert.ok(markup.includes('每组 <b>2 条</b>'));assert.ok(markup.includes('时长 <b>15 秒</b>'));assert.ok(markup.includes('本机 ComfyUI'));
assert.ok(markup.includes('2 条视频生成'));assert.ok(markup.includes('可能产生费用'));
assert.doesNotThrow(()=>assertRecipeReviewSnapshot(batch,'confirm_generate'));
const missingEffective={...batch,items:[{...batch.items[0],effective_workflow:null}]};
assert.throws(()=>assertRecipeReviewSnapshot(missingEffective,'confirm_generate'),/生成设置还未同步/);
assert.doesNotThrow(()=>assertRecipeReviewSnapshot(missingEffective,'confirm_reverse'));
assert.equal(recipeCan(batch,'confirm_generate'),true);
assert.equal(recipeCan(batch,'pause'),false);
assert.equal(recipeRequest('same',{revision:1}),recipeRequest('same',{revision:1}));
assert.notEqual(recipeRequest('same',{revision:1}),recipeRequest('same',{revision:2}));
// 产物在类型可用前只做占位缩略图 + 打开/保存，不渲染不可用的播放器（Q1）。
const image=recipeOutputMarkup({path:'managed/out.png',name:'<unsafe>',type:'image'});
assert.ok(image.includes('/api/file?token=auth'));assert.ok(image.includes('kind-image'));assert.ok(!image.includes('<img'));assert.ok(!image.includes('<video'));assert.ok(!image.includes('<audio'));assert.ok(!image.includes('<unsafe>'));
assert.ok(!recipeOutputMarkup({path:'managed/out.mp4',type:'video'}).includes('<video'));
assert.ok(recipeOutputMarkup({path:'managed/out.mp4',name:'C:'+String.fromCharCode(92)+'outputs'+String.fromCharCode(92)+'clip.mp4',type:'video'}).includes('打开 / 保存 clip.mp4'));
assert.ok(!recipeOutputMarkup({path:'managed/unknown.bin',type:'file'}).includes('<img'));
const draft=recipeDraft(batch);draft.dirty=true;draft.revision=1;draft.items={one:{prompt:'edited prompt',included:false}};draft.baseline={one:{prompt:'old prompt',included:true}};
context.nextBatch={...batch,revision:2,revision_hash:'hash-2',items:[{...batch.items[0],prompt:'edited prompt',included:false},batch.items[1]]};
viewState.selectedId='thread';recipeState.batches.set('thread',batch);renderRecipeBatch=()=>{};
await recipeAction('confirm_generate');
assert.equal(requests.length,2);assert.equal(requests[0].body.action,'edit');assert.equal(requests[0].body.items.one.prompt,'edited prompt');assert.equal(requests[0].body.items.one.included,false);assert.equal(requests[0].body.revision,1);
assert.ok(requests[1].path.endsWith('/recipe/precheck'));
assert.equal(recipeState.review.batch.revision,2);assert.equal($('#recipeReviewDialog').open,true);
context.nextBatch={...context.nextBatch,status:'generation_running',revision:3};await confirmRecipeReview();
assert.equal(requests.length,3);assert.equal(requests[2].body.action,'confirm_generate');assert.equal(requests[2].body.revision,2);assert.equal(requests[2].body.revision_hash,'hash-2');
const stale=recipeDraft({...batch,thread_id:'stale'});stale.dirty=true;stale.revision=1;stale.items={one:{prompt:'mine'}};stale.baseline={one:{prompt:'old'}};
await assert.rejects(()=>saveRecipeDraft({...batch,thread_id:'stale',revision:5}),/服务器版本已变化/);
assert.equal(stale.items.one.prompt,'mine');
const numeric=recipeNumericSlots({workflow:{'1':{inputs:{seed:7,width:768,prompt:'text',value:15,image:'x'}}},prompt_slot:{node_id:'1',field_name:'prompt'},duration_slot:{node_id:'1',field_name:'value'},media_slots:[{node_id:'1',field_name:'image'}]});
assert.equal(numeric.length,2);assert.ok(numeric.some(slot=>slot.field_name==='seed'));
const fixedIntent={workflow:{prompt:{'1':{inputs:{seed:7,enabled:false,path:'saved-path',sampler_name:'euler',model:'saved-model',prompt:'saved prompt'}}},params:[{node_id:'1',field_name:'seed',field_type:'seed'},{node_id:'1',field_name:'enabled',field_type:'bool'},{node_id:'1',field_name:'path',field_type:'text'},{node_id:'1',field_name:'sampler_name',field_type:'text'},{node_id:'1',field_name:'model',field_type:'text'},{node_id:'1',field_name:'prompt',field_type:'text'}]},overrides:[{node_id:'1',field_name:'seed',value:'42'},{node_id:'1',field_name:'enabled',value:'true'},{node_id:'1',field_name:'path',value:'saved-path'},{node_id:'1',field_name:'sampler_name',value:'euler'},{node_id:'1',field_name:'model',value:'saved-model'}]};
const fixed=recipeCustomFixedOverrides(fixedIntent);
assert.equal(fixed.length,2);assert.equal(fixed[0].value,42);assert.equal(fixed[1].value,true);
// 文本/枚举参数：变了的原样带上（映射用户定），没变的跳过，不再按类型拦。
const changedText=recipeCustomFixedOverrides({...fixedIntent,overrides:[{node_id:'1',field_name:'path',value:'changed-path'},{node_id:'1',field_name:'sampler_name',value:'dpmpp'},{node_id:'1',field_name:'model',value:'changed-model'}]});
assert.deepEqual(changedText.map(row=>[row.field_name,row.value]),[['path','changed-path'],['sampler_name','dpmpp'],['model','changed-model']]);
// 未知槽位仍要拦：值写不进任何地方。
assert.throws(()=>recipeCustomFixedOverrides({...fixedIntent,overrides:[{node_id:'9',field_name:'nope',value:'x'}]}),/不在这份工作流的参数里/);
// 媒体槽不接受文本覆盖，要走素材入口。
const mediaIntent={...fixedIntent,overrides:[{node_id:'1',field_name:'image',value:'x.png'}],workflow:{...fixedIntent.workflow,params:[...fixedIntent.workflow.params,{node_id:'1',field_name:'image',field_type:'image'}]}};
assert.throws(()=>recipeCustomFixedOverrides(mediaIntent),/素材入口/);
const managedIntent={...fixedIntent,overrides:[...fixedIntent.overrides,{node_id:'1',field_name:'prompt',value:'new per-item prompt'}]};
assert.equal(recipeCustomFixedOverrides(managedIntent,new Set(['1|prompt'])).length,2);
assert.equal(recipeCustomFixedOverrides(managedIntent).length,3);
// 交接二选一（Q3 口径）：「已保存的默认值」不带 overrides，正文取工作流保存值；
// 「当前未保存调整」只带数值 / 开关覆盖，正文取当前值；张数沿用造梦机 shots。
const handoffIntent={workflow:{id:'cw-9',name:'金鱼',workflow_fingerprint:'fp-9',prompt:{'1':{inputs:{text:'默认正文'}},'5':{inputs:{value:15}},'6':{inputs:{width:832}},'7':{inputs:{height:1216}}},params:[{node_id:'1',field_name:'text',field_type:'text',control:'multiline'},{node_id:'5',field_name:'value',field_type:'number',node_title:'视频时长（秒）'},{node_id:'6',field_name:'width',field_type:'number',role:'resolution'},{node_id:'7',field_name:'height',field_type:'number',role:'resolution'}]},overrides:[{node_id:'1',field_name:'text',value:'当前正文'},{node_id:'5',field_name:'value',value:5}],media_inputs:[{node_id:'3',field_name:'image',path:'managed/a.png'}],shots:2};
await openRecipeWorkflowIntent(handoffIntent);
assert.equal($('#superAgentHandoffDialog').open,true);
assert.equal(recipeState.handoff.mode,'saved');
// 交接摘要必须亮出时长 / 分辨率的实际值与来源（2026-11-04 反馈）：
const summarySaved=$('#superAgentHandoffSummary').innerHTML;
assert.ok(summarySaved.includes('<b>时长</b> · 15 秒 · 沿用已保存值'),summarySaved);
assert.ok(summarySaved.includes('<b>分辨率 / 画幅</b> · 宽 832 · 高 1216 · 沿用已保存值'),summarySaved);
assert.ok($('#superAgentHandoffChanges').textContent.includes('5 / value：15 → 5') === false,$('#superAgentHandoffChanges').textContent);
// 时长槽已映射：时长变化升格为批次时长（摘要「时长」行亮出），不再进固定参数覆盖。
recipeState.handoff.mode='current';renderHandoffSummary();
assert.ok($('#superAgentHandoffSummary').innerHTML.includes('<b>时长</b> · 5 秒 · 本次未保存调整'),$('#superAgentHandoffSummary').innerHTML);
// 没有时长槽 / 分辨率槽的工作流：如实说「按工作流原配置出片」，不冒充有值。
await openRecipeWorkflowIntent({...handoffIntent,overrides:[],media_inputs:[],workflow:{...handoffIntent.workflow,params:handoffIntent.workflow.params.filter(slot=>slot.role!=='resolution'&&slot.node_id!=='5')}});
const summaryBare=$('#superAgentHandoffSummary').innerHTML;
assert.ok(summaryBare.includes('<b>时长</b> · 未映射时长槽'),summaryBare);
assert.ok(summaryBare.includes('<b>分辨率 / 画幅</b> · 工作流未暴露分辨率 / 画幅参数'),summaryBare);
// H3 这类解析度選擇器工作流：aspect_ratio 走字段名识别，max_resolution（预览覆盖）不算。
await openRecipeWorkflowIntent({...handoffIntent,overrides:[],media_inputs:[],workflow:{...handoffIntent.workflow,params:[handoffIntent.workflow.params[0],{node_id:'8',field_name:'aspect_ratio',field_type:'text',node_title:'解析度選擇器'},{node_id:'9',field_name:'max_resolution',field_type:'number',node_title:'Model Preview Override'}],prompt:{...handoffIntent.workflow.prompt,'8':{inputs:{aspect_ratio:'9:16 (Portrait Widescreen)'}}}}});
const summaryAspect=$('#superAgentHandoffSummary').innerHTML;
assert.ok(summaryAspect.includes('<b>分辨率 / 画幅</b> · aspect_ratio 9:16 (Portrait Widescreen) · 沿用已保存值'),summaryAspect);
assert.ok(!summaryAspect.includes('max_resolution'),summaryAspect);
await openRecipeWorkflowIntent(handoffIntent);
await confirmWorkflowHandoff();
const handoffCreate=requests.filter(row=>row.path==='/api/super-agent/recipe-batches').at(-1);
assert.equal(handoffCreate.body.source,'workflow');assert.equal(handoffCreate.body.prompt,'默认正文');assert.ok(!('overrides' in handoffCreate.body));assert.equal(handoffCreate.body.shots,2);
assert.equal(handoffCreate.body.media_inputs.length,1);assert.equal(handoffCreate.body.media_inputs[0].node_id,'3');assert.equal(handoffCreate.body.media_inputs[0].path,'managed/a.png');
recipeState.handoff={intent:handoffIntent,mapping:{prompt_slot:{node_id:'1',field_name:'text'},media_slots:[],duration_slot:{node_id:'5',field_name:'value'}},fingerprint:'fp-9',inputMode:'prompt',templateId:'recipe_minimax_h3_r2v_v2',mode:'current',changed:[{node_id:'5',field_name:'value',value:5}],changedError:''};
await confirmWorkflowHandoff();
const handoffCreate2=requests.filter(row=>row.path==='/api/super-agent/recipe-batches').at(-1);
assert.equal(handoffCreate2.body.prompt,'当前正文');assert.equal(handoffCreate2.body.overrides.length,1);assert.equal(handoffCreate2.body.overrides[0].value,5);assert.equal(handoffCreate2.body.overrides[0].field_name,'value');
})().catch(error=>{console.error(error);process.exitCode=1;});
`,Object.assign(context,{requests,context}));
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
