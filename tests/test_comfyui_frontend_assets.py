"""ComfyUI 前端资产契约 + 任务总管六类内容区结构；DOM-free，不开浏览器。

（原 test_recipe_frontend_assets.py 的通用部分：配方线删除后保留改造，
交档 §六「若还有通用断言则保留改造」。）
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ComfyUIFrontendAssets(unittest.TestCase):
    def test_comfyui_page_has_no_cross_domain_coupling(self):
        comfy = (ROOT / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        self.assertNotIn("superAgentView", comfy)
        self.assertNotIn("/api/super-agent", comfy)

    def test_markup_ids_unique_and_mobile_media_controls_scoped(self):
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))
        for name in ("comfyuiDraftHint", "comfyuiSaveAsDialog", "comfyuiSaveAsList",
                     "comfyuiSaveAsDiscard", "comfyuiSaveAsConfirm"):
            self.assertIn(name, ids)
        self.assertIn('webkitdirectory', html)
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        self.assertIn(".comfyui-draft-hint", css)
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

    def test_launch_dialog_is_independent_and_type_cards_route(self):
        # 启动台是独立 dialog；五张类型卡（图标 + 前置条件标签）不在
        # 新建表单里；隐藏的类型 select 继续当真源。
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        source = (ROOT / "public/js/28-super-agent.js").read_text(encoding="utf-8")

        class DialogParents(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.parents = {}
                self.type_cards = 0

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get("class") and "type-card" in attrs["class"] and attrs.get("data-launch-domain"):
                    self.type_cards += 1
                if attrs.get("id") in {"superAgentLaunchCards", "superAgentCreateDomain"}:
                    self.parents[attrs["id"]] = list(self.stack)
                if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                    self.stack.append((tag, attrs.get("id")))

            def handle_endtag(self, tag):
                for index in range(len(self.stack) - 1, -1, -1):
                    if self.stack[index][0] == tag:
                        del self.stack[index:]
                        break

        parser = DialogParents()
        parser.feed(html)
        self.assertEqual(parser.type_cards, 5, "启动台应有 5 张类型卡")
        launch_chain = [node_id for tag, node_id in parser.parents["superAgentLaunchCards"] if node_id]
        self.assertIn("superAgentLaunchDialog", launch_chain)
        self.assertNotIn("superAgentCreateForm", launch_chain)
        # 类型真源 select 仍在新建表单里（隐藏）。
        create_chain = [node_id for tag, node_id in parser.parents["superAgentCreateDomain"] if node_id]
        self.assertIn("superAgentCreateForm", create_chain)
        self.assertNotIn("superAgentLaunchpad", html)
        self.assertNotIn('data-launch-domain="recipe"', html)
        self.assertIn('id="superAgentLaunchDialog"', html)
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        for rule in (".type-card", ".tc-ico", ".tc-meta", ".lg-head", ".super-agent-launch-dialog .type-grid"):
            self.assertIn(rule, css)
        # 交互：＋新建任务 → 启动台；选卡关启动台并路由。
        self.assertIn('function openLaunchDialog()', source)
        self.assertIn('$("#superAgentCreate")?.addEventListener("click", () => openLaunchDialog())', source)
        self.assertIn('$("#superAgentLaunchDialog")?.close()', source)
        self.assertIn('void openCreateDomainDialog({ domain })', source)

    def test_content_zone_structures_in_product_assets(self):
        # 六类内容区按预览稿结构落进产品资产（配方批次页已随配方线删除）。
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        source = (ROOT / "public/js/28-super-agent.js").read_text(encoding="utf-8")
        self.assertIn('class="super-agent-section content-zone" id="superAgentContentZone"', html)
        self.assertIn('id="superAgentStageSection"', html)
        for rule in (".content-zone", ".stage-track", ".track-node", ".chapter-grid", ".sec-head",
                     ".goal-card", ".kv-row", ".mirror-banner", ".source-card", ".instruction",
                     ".delegate-grid", ".approval", ".param-mask", ".gate-card", ".pill.ok"):
            self.assertIn(rule, css)
        # 五类各自的结构签名都在渲染代码里。
        signatures = {
            "writer": ("stage-track", "chapter-grid", "row-list"),
            "novel": ("track-5", "gate-card"),
            "chat": ("goal-card", "mode-cards", "kv-row"),
            "job": ("mirror-banner", "source-card"),
            "subagent": ("instruction", "delegate-grid", "approval", "param-mask"),
        }
        for kind, marks in signatures.items():
            for mark in marks:
                self.assertIn(mark, source, f"{kind} 内容区缺少结构 {mark}")
        self.assertNotIn("zone-track-node", source)
        self.assertNotIn("zone-track-node", css)
        # 窄屏与深浅主题：新增结构有降列规则，颜色全部走产品变量（不硬编码浅色底）。
        self.assertNotIn("linear-gradient(135deg,#F", css)

    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_render_content_zone_five_kinds_execute_distinct(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/28-super-agent.js','utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
const nodes=new Map();
const makeNode=()=>({innerHTML:'',hidden:false,children:[],dataset:{},classList:{add(){},remove(){},toggle(){}},querySelector(){return null},querySelectorAll(){return []}});
const $=key=>{if(!nodes.has(key))nodes.set(key,makeNode());return nodes.get(key)};
const context={assert,console,setTimeout,clearTimeout,$,document:{activeElement:null},escapeHtml:value=>String(value??''),localFileUrl:path=>path,state:{}};
vm.runInNewContext(source+`
const node=(over={})=>({node_id:over.node_id||'n1',kind:over.kind||'step',stage_key:over.stage_key||'',status:over.status||'pending',executor_type:over.executor_type||'',dependencies:[],capabilities:{},error:'',domain_ref:over.domain_ref||null,attempt:0});
const artifacts=()=>[{title:'第 1 章 · 起点',kind:'doc',created_at:1},{title:'第 2 章 · 雨夜',kind:'doc',created_at:2}];
const marksFor=(kind,detail,nodes2,artifacts2,approvals)=>{
  viewState.detail=detail;viewState.artifacts=artifacts2||[];viewState.approvals=approvals||[];
  renderContentZone(kind,detail,nodes2, '');
  return $('#superAgentContentZone').innerHTML;
};
const writerNodes=[node({executor_type:'writer_pipeline',stage_key:'setup',status:'succeeded'}),node({executor_type:'writer_pipeline',stage_key:'outline',status:'succeeded'}),node({executor_type:'writer_pipeline',stage_key:'chapter_plan',status:'running'}),node({executor_type:'writer_pipeline',stage_key:'chapter_content'})];
const novelNodes=['import','elements','outline','screenplay','review'].map((key,index)=>node({executor_type:'novel_stage',stage_key:key,status:index<3?'succeeded':index===3?'running':'waiting_user'}));
const chatDetail={goal:'把导出补齐',supervision:{round:2,max_rounds:4,remaining_continuations:2,review_count:2}};
const jobNodes=[node({executor_type:'job'})];
const subNodes=[node({executor_type:'subagent',status:'succeeded'}),node({executor_type:'subagent',status:'running'})];
const writer=marksFor('writer',{goal:'g'},writerNodes,artifacts());
assert.ok(writer.includes('stage-track')&&writer.includes('track-4')&&writer.includes('track-node done'));
assert.ok(writer.includes('chapter-grid')&&writer.includes('chapter done')&&writer.includes('row-list'));
const novel=marksFor('novel',{goal:'g'},novelNodes,artifacts());
assert.ok(novel.includes('track-5')&&novel.includes('gate')&&novel.includes('gate-card'));
const chat=marksFor('chat',chatDetail,[node({executor_type:'chat_run'})],[]);
assert.ok(chat.includes('goal-card')&&chat.includes('mode-cards')&&chat.includes('mode-card active')&&chat.includes('kv-row'));
const job=marksFor('job',{goal:'g',linked_job_ids:['j1']},jobNodes,[]);
assert.ok(job.includes('mirror-banner')&&job.includes('source-card'));
assert.ok(!job.includes('track-node'), 'job 是只读镜像，不出现本地步骤轨道');
const approvals=[{approval_id:'ap1',status:'pending',action:'写入文件',description:'保存润色稿'}];
const sub=marksFor('subagent',{goal:'整理三章'},subNodes,[],approvals);
assert.ok(sub.includes('instruction')&&sub.includes('delegate-grid')&&sub.includes('approval')&&sub.includes('param-mask'));
assert.ok(sub.includes('data-super-approval-id="ap1"'));
// 回退：未知类型不渲染内容区。
marksFor('',{goal:'g'},[node({executor_type:'legacy'})],[]);
assert.equal($('#superAgentContentZone').hidden,true);
assert.equal($('#superAgentContentZone').innerHTML,'');
// 五类结构互异（剥掉角标文字后正文长度与开头结构不同）。
const sigs=[writer,novel,chat,job,sub].map(markup=>markup.replace(/内容区 [A-F][^<]*/g,'').length);
assert.equal(new Set(sigs).size,5, '五类内容区结构应互异：'+sigs.join(','));
`,Object.assign(context,{}));
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_batch_submission_shape_mapping_resolution_and_convert(self):
        # 批量生成（方案 A）：提交 body 形状——媒体走 media_inputs 且已按映射
        # 套好具体槽位地址；固定参数只带与保存值不同的项；未映射不写入。
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/24-comfyui.js','utf8');
(async()=>{
const nodes=new Map(),requests=[],toasts=[];
const makeNode=()=>({value:'',checked:false,disabled:false,hidden:false,open:false,dataset:{},textContent:'',innerHTML:'',options:[],children:[],classList:{add(){},remove(){},toggle(){}},setAttribute(){},removeAttribute(){},addEventListener(){},append(){},closest(){return null},querySelector(){return null},querySelectorAll(){return []}});
const $=key=>{if(!nodes.has(key))nodes.set(key,makeNode());return nodes.get(key)};
const context=vm.createContext({console,setTimeout,clearTimeout,URL,document:{createElement:()=>makeNode(),querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},activeElement:null,body:{append(){}},addEventListener(){}},window:{addEventListener(){}},localStorage:{setItem(){},getItem:()=>null},CSS:{escape:value=>String(value)}});
const values={$,toast:message=>toasts.push(String(message)),state:{providerModelCatalogs:{},bootstrap:{model_profiles:[{model_key:'prov',name:'P',model:'m1'}]}},api:async(path,options)=>{requests.push({path,body:options?.body?JSON.parse(JSON.stringify(options.body)):null});
  if(path==='/api/comfyui/batches')return {batch:{id:'cfb-x1'}};
  return {};}};
async function make(name,sourceText,exportsLine){
  const byMod=new Map();
  for(const match of sourceText.matchAll(/^import \{([^}]+)\} from "([^"]+)"/gm)){
    byMod.set(match[2], match[1].split(',').map(n=>n.trim()).filter(Boolean));
  }
  const mod=new vm.SourceTextModule(sourceText+exportsLine,{context});
  await mod.link(async (spec)=>{
    const names=byMod.get(spec)||[];
    return new vm.SyntheticModule(names,function(){for(const n of names)this.setExport(n,values[n]??(()=>{}));},{context});
  });
  await mod.evaluate();
  return mod.namespace;
}
const m=await make('comfy',source,'\nexport {batch,ensureBatchDraft,submitBatch,batchPortOrdinal,batchSlotAddressText,batchPortsMarkup,batchItemsMarkup};');
const {batch,ensureBatchDraft,submitBatch,batchPortOrdinal,batchSlotAddressText,batchPortsMarkup,batchItemsMarkup}=m;
ensureBatchDraft();
const s=batch.draft.schemes[0];
s.detail={id:'cw-1',name:'测试流',workflow_fingerprint:'fp-x',
  prompt:{'5':{inputs:{text:'默认正文'}},'6':{inputs:{length:5}},'7':{inputs:{res:'848x480'}},'8':{inputs:{image:'old.png'}},'9':{inputs:{lora:'base.safetensors'}}},
  params:[{node_id:'5',field_name:'text',field_type:'text',control:'multiline',role:'positive_prompt',label:'正文'},
          {node_id:'6',field_name:'length',field_type:'number',default:5,label:'时长'},
          {node_id:'7',field_name:'res',field_type:'text',candidates:['848x480','480x848'],label:'分辨率'},
          {node_id:'8',field_name:'image',field_type:'image',control:'media',media:'image',node_title:'载入图片',label:'8 · 载入图片 · image → ref_images.ref_image_0',media_optional:true},
          {node_id:'10',field_name:'image',field_type:'image',control:'media',media:'image',node_title:'载入图片',label:'10 · 载入图片 · image → ref_images.ref_image_1、mask（节点 21）',media_optional:true},
          {node_id:'9',field_name:'lora',field_type:'text',role:'lora',label:'LoRA'}]};
s.workflow_id='cw-1';
s.mapping={prompt_slot:{node_id:'5',field_name:'text'},duration_slot:{node_id:'6',field_name:'length'},
  resolution_slot:{node_id:'7',field_name:'res'},
  media_ports:[{node_id:'8',field_name:'image',checked:true}]};
// 同名加载节点必须保留与单次生成相同的完整下游标签；地址值不随显示文案改变。
assert.equal(batchSlotAddressText(s.detail,{node_id:'8',field_name:'image'}),s.detail.params.find(row=>row.node_id==='8').label);
const portsMarkup=batchPortsMarkup(s);
assert.ok(portsMarkup.includes('value="8|image" selected'));
assert.ok(portsMarkup.includes('8 · 载入图片 · image → ref_images.ref_image_0'));
assert.ok(portsMarkup.includes('10 · 载入图片 · image → ref_images.ref_image_1、mask（节点 21）'));
assert.equal(batchSlotAddressText(s.detail,{node_id:'missing',field_name:'image'}),'missing · image');
s.fixed={'9|lora':'other.safetensors'};
s.items=[{name:'条目 1',script:'',prompt:'场景A',duration:'8',resolution:'848x480',shots:1,
          files:{'0':{path:'/managed/a.png',name:'a.png',thumb_path:''}}},
         {name:'条目 2',script:'',prompt:'场景B',duration:'',resolution:'',shots:2,files:{}}];
// 执行真实条目渲染，覆盖分辨率枚举和独立索引；仅提交形状测试无法发现作用域错误。
let itemMarkup=batchItemsMarkup(s,0);
assert.ok(itemMarkup.includes('data-batch-item="resolution"'));
assert.ok(itemMarkup.includes('data-item="0"'));
assert.ok(itemMarkup.includes('data-item="1"'));
assert.ok(itemMarkup.includes('value="848x480" selected'));
const resolutionMapping=s.mapping.resolution_slot;
s.mapping.resolution_slot=null;
itemMarkup=batchItemsMarkup(s,0);
assert.ok(itemMarkup.includes('type="text" step="any" placeholder="按工作流"'));
s.mapping.resolution_slot=resolutionMapping;
batch.draft.title='验收批次';batch.draft.unload_after=true;
await submitBatch();
const post=requests.find(row=>row.path==='/api/comfyui/batches');
assert.ok(post,'必须 POST /api/comfyui/batches');
assert.equal(post.body.title,'验收批次');assert.equal(post.body.unload_after,true);
const scheme=post.body.schemes[0];
assert.equal(scheme.workflow_id,'cw-1');
assert.deepEqual(scheme.mapping,{prompt_slot:{node_id:'5',field_name:'text'},duration_slot:{node_id:'6',field_name:'length'},resolution_slot:{node_id:'7',field_name:'res'}});
assert.deepEqual(scheme.overrides,[{node_id:'9',field_name:'lora',value:'other.safetensors'}]);
const [one,two]=scheme.items;
assert.equal(one.prompt,'场景A');assert.equal(one.duration_seconds,8);assert.equal(one.resolution,'848x480');assert.equal(one.shots,1);
assert.deepEqual(one.media_inputs,[{node_id:'8',field_name:'image',path:'/managed/a.png',media:'image'}]);
assert.equal(two.duration_seconds,null);assert.equal(two.resolution,'');assert.equal(two.media_inputs.length,0);
assert.equal(two.shots,2);
// 换节点/取消勾选后条目文件键按已勾选口序重排：ordinal 语义测试。
const b=batch.draft.schemes[0];
b.mapping.media_ports.push({node_id:'9x',field_name:'audio',checked:false});
assert.equal(b.items[0].files['0'].path,'/managed/a.png');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(["node", "--experimental-vm-modules", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_results_page_two_segments_split_single_and_batch(self):
        # 结果页两档（单次 / 批量）：批量条目不再混进单次任务与产物网格，
        # 历史批次入口统一到「批量」档——填单里的旧历史下拉必须删干净。
        js = (ROOT / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        self.assertNotIn('data-batch-select="history"', js)
        self.assertNotIn("openBatchTaskOutputs", js)
        self.assertNotIn("cbm-history", css)
        self.assertIn('id="comfyuiResultsSeg"', html)
        self.assertIn('id="comfyuiBatchList"', html)
        self.assertIn("data-results-segment", html)
        self.assertIn("function setComfyuiResultsSegment(", js)
        self.assertIn("function isBatchTaskRow(", js)
        self.assertIn("function renderBatchHistoryList()", js)
        self.assertIn("comfyui.tasks.filter((row) => !isBatchTaskRow(row))", js)
        self.assertIn("if (isBatchTaskRow(row)) continue;", js)
        for rule in (".comfyui-results-seg", ".comfyui-batch-row", ".comfyui-batch-progress",
                     ".cbm-card-expand", ".cbm-expand-empty"):
            self.assertIn(rule, css)
        self.assertIn('[data-results-segment="single"] .comfyui-batch-history', css)
        self.assertIn('[data-results-segment="batch"] .comfyui-tasks', css)
        self.assertIn('[data-results-segment="batch"] .comfyui-outputs', css)
        # 跑批视图：排队/生成中的条目卡有取消，完成条目产物就地展开。
        self.assertIn('data-batch-action="cancel-item"', js)
        self.assertIn('data-batch-action="toggle-item-outputs"', js)
        self.assertIn("function mountBatchItemOutputs()", js)
        self.assertNotIn('data-batch-action="open-task-outputs"', js)

    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_results_segments_and_batch_history_render(self):
        # 批量档渲染契约：徽标口径、失败进度条、行点击目标、空态；跑批视图
        # 三种条目卡各自只带自己的动作（取消 / 产物就地展开 / 重试）。
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/24-comfyui.js','utf8');
(async()=>{
const nodes=new Map();
const makeNode=()=>({value:'',checked:false,disabled:false,hidden:false,dataset:{},textContent:'',innerHTML:'',children:[],classList:{add(){},remove(){},toggle(){}},setAttribute(){},append(){},closest(){return null},querySelector(){return null},querySelectorAll(){return []}});
const $=key=>{if(!nodes.has(key))nodes.set(key,makeNode());return nodes.get(key)};
const context=vm.createContext({console,setTimeout,clearTimeout,URL,document:{createElement:()=>makeNode(),querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},activeElement:null,body:{append(){}},addEventListener(){}},window:{addEventListener(){}},localStorage:{setItem(){},getItem:()=>null},CSS:{escape:value=>String(value)}});
const values={$,toast:()=>{},state:{},localFileUrl:path=>path,api:async()=>({})};
async function make(name,sourceText,exportsLine){
  const byMod=new Map();
  for(const match of sourceText.matchAll(/^import \{([^}]+)\} from "([^"]+)"/gm)){
    byMod.set(match[2], match[1].split(',').map(n=>n.trim()).filter(Boolean));
  }
  const mod=new vm.SourceTextModule(sourceText+exportsLine,{context});
  await mod.link(async (spec)=>{
    const names=byMod.get(spec)||[];
    return new vm.SyntheticModule(names,function(){for(const n of names)this.setExport(n,values[n]??(()=>{}));},{context});
  });
  await mod.evaluate();
  return mod.namespace;
}
const m=await make('comfy',source,'\nexport {batch,batchHistoryBadge,renderBatchHistoryList,batchRunCardMarkup,isBatchTaskRow};');
const {batch,batchHistoryBadge,renderBatchHistoryList,batchRunCardMarkup,isBatchTaskRow}=m;
assert.equal(isBatchTaskRow({batch_id:'cfb-a1'}),true);
assert.equal(isBatchTaskRow({batch_id:''}),false);
assert.equal(isBatchTaskRow({}),false);
assert.equal(isBatchTaskRow(null),false);
// 徽标数组跨 VM 域比较原型不等，用 JSON 字符串比对形状。
assert.equal(JSON.stringify(batchHistoryBadge({items:[{status:'running'},{status:'completed'}],done_count:1,item_count:2})),JSON.stringify(['run','正在生成 1/2']));
assert.equal(JSON.stringify(batchHistoryBadge({items:[{status:'completed'},{status:'completed'}],done_count:2,item_count:2})),JSON.stringify(['done','2 条全部完成']));
assert.equal(JSON.stringify(batchHistoryBadge({items:[{status:'failed'},{status:'completed'},{status:'cancelled'}],done_count:1,item_count:3})),JSON.stringify(['fail','1/3 完成 · 2 失败']));
batch.histories=[
  {id:'cfb-a0',title:'短剧第2集',done_count:3,item_count:6,created_at:0,items:[{status:'running'},{status:'completed'},{status:'completed'}]},
  {id:'cfb-a1',title:'短剧第1集',done_count:6,item_count:6,created_at:new Date('2026-11-06T13:05:00').getTime(),items:[{status:'completed'}]},
  {id:'cfb-a2',title:'短剧第3集',done_count:2,item_count:6,created_at:0,items:[{status:'failed'},{status:'failed'},{status:'completed'}]}];
renderBatchHistoryList();
const html=$('#comfyuiBatchList').innerHTML;
assert.ok(html.includes('data-batch-open="cfb-a1"')&&html.includes('短剧第1集'));
assert.ok(html.includes('cbm-badge done')&&html.includes('6 条全部完成'));
assert.ok(html.includes('is-failed')&&html.includes('cbm-badge fail'));
assert.ok(html.includes('正在生成'));
batch.histories=[];
renderBatchHistoryList();
assert.ok($('#comfyuiBatchList').innerHTML.includes('还没有批次'));
const running=batchRunCardMarkup({index:1,name:'条目 1',status:'running',done:1,total:3,task_id:'ct-9',outputs:[]});
assert.ok(running.includes('data-batch-action="cancel-item"')&&running.includes('data-task-id="ct-9"'));
assert.ok(!running.includes('open-task-outputs'));
batch.expandedItems.add('ct-10');
const done=batchRunCardMarkup({index:2,name:'条目 2',status:'completed',done:2,total:2,task_id:'ct-10',outputs:['a.png','b.png']});
assert.ok(done.includes('data-batch-action="toggle-item-outputs"')&&done.includes('aria-expanded="true"'));
assert.ok(done.includes('data-batch-expand-for="ct-10"'));
const failed=batchRunCardMarkup({index:3,name:'条目 3',status:'failed',error:'超时',task_id:'ct-11',outputs:[]});
assert.ok(failed.includes('data-batch-action="retry-item"')&&!failed.includes('cancel-item'));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(["node", "--experimental-vm-modules", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
