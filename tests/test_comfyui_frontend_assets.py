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

    def test_media_overlay_foreground_does_not_follow_page_text(self):
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        for selector in (".comfyui-media-tile .t-actions button",
                         ".comfyui-media-tile .t-duration",
                         ".comfyui-media-tile .t-status",
                         ".comfyui-media-preview-close"):
            with self.subTest(selector=selector):
                rule = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", css)
                self.assertIsNotNone(rule)
                self.assertRegex(rule.group(1), r"\bcolor:\s*#fff\s*;")
        self.assertIn(".comfyui-media-tile .t-status.ok { color: var(--success); }", css)
        self.assertIn(".comfyui-media-tile .t-status.keep { color: var(--warning); }", css)
        source = (ROOT / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        icon = re.search(r"const MEDIA_PLAY_ICON = '([^']+)';", source)
        self.assertIsNotNone(icon)
        self.assertIn('fill="currentColor"', icon.group(1))

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


    def test_batch_detail_stays_in_results_and_uses_persisted_elapsed(self):
        # 从结果页批次列表打开详情：留在结果区（tasks 档），不切回批量生成填单。
        js = (ROOT / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        start = js.index("function openBatchHistory(id) {")
        block = js[start:js.index("\n}", start)]
        self.assertIn("batch.resultsDetailId = String(id);", block)
        self.assertIn('setComfyuiPane("tasks");', block)
        self.assertNotIn('setComfyuiPane("batch");', block)
        self.assertIn('id="comfyuiBatchDetail"', html)
        self.assertIn('data-batch-detail="0"', html)
        # 耗时只认服务端持久时间：不再从打开详情的时刻起算，也不对完成批次继续累计。
        self.assertNotIn("batch.runStartedAt", js)
        self.assertIn("function batchElapsedText(data, now = Date.now())", js)
        self.assertIn("if (finished) return `耗时", js)
        self.assertIn('if (!active) return "耗时未知"', js)
        self.assertIn("started_at: Number(data?.started_at) || 0", js)
        self.assertIn("finished_at: Number(data?.finished_at) || 0", js)

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
const m=await make('comfy',source,'\nexport {batch,ensureBatchDraft,submitBatch,batchPortOrdinal,batchSlotAddressText,batchPortsMarkup,batchItemsMarkup,batchFixedMarkup,batchSetFixedValue,batchJsonParamMarkup,batchItemRowMarkup,jsonParamInfo,jsonParamSummary,jsonParamButtonText,jsonParamHelper,jsonEditableValue,jsonNewEntry,jsonTargetAssign};');
const {batch,ensureBatchDraft,submitBatch,batchPortOrdinal,batchSlotAddressText,batchPortsMarkup,batchItemsMarkup,batchFixedMarkup,batchSetFixedValue,batchJsonParamMarkup,batchItemRowMarkup,jsonParamInfo,jsonParamSummary,jsonParamButtonText,jsonParamHelper,jsonEditableValue,jsonNewEntry,jsonTargetAssign}=m;
ensureBatchDraft();
const s=batch.draft.schemes[0];
s.detail={id:'cw-1',name:'测试流',workflow_fingerprint:'fp-x',
  prompt:{'5':{inputs:{text:'默认正文'}},'6':{inputs:{length:5}},'7':{inputs:{res:'848x480'}},'8':{inputs:{image:'old.png'}},'9':{inputs:{lora:'base.safetensors'}},'20':{inputs:{lora_data:'[{"name":"a","strength_model":1,"strength_clip":1,"enabled":true},{"name":"b","strength_model":0.5,"strength_clip":1,"enabled":false}]'}},'31':{inputs:{tail:''}}},
  params:[{node_id:'5',field_name:'text',field_type:'text',control:'multiline',role:'positive_prompt',label:'正文'},
          {node_id:'6',field_name:'length',field_type:'number',default:5,label:'时长'},
          {node_id:'7',field_name:'res',field_type:'text',candidates:['848x480','480x848'],label:'分辨率'},
          {node_id:'8',field_name:'image',field_type:'image',control:'media',media:'image',node_title:'载入图片',label:'8 · 载入图片 · image → ref_images.ref_image_0',media_optional:true},
          {node_id:'10',field_name:'image',field_type:'image',control:'media',media:'image',node_title:'载入图片',label:'10 · 载入图片 · image → ref_images.ref_image_1、mask（节点 21）',media_optional:true},
          {node_id:'9',field_name:'lora',field_type:'text',role:'lora',label:'LoRA'},
          {node_id:'11',field_name:'cache',field_type:'bool',default:true,label:'缓存'},
          {node_id:'12',field_name:'steps',field_type:'number',default:8,label:'步数'},
          {node_id:'20',field_name:'lora_data',field_type:'text',label:'20 · Visual LoRA Loader · lora_data'},
          {node_id:'31',field_name:'tail',field_type:'text',control:'multiline',label:'第二段'}]};
s.workflow_id='cw-1';
s.mapping={prompt_slot:{node_id:'5',field_name:'text'},duration_slot:{node_id:'6',field_name:'length'},
  resolution_slot:{node_id:'7',field_name:'res'},
  extra_points:[{id:'bx1',kind:'prompt',slot:{node_id:'31',field_name:'tail'}}],
  media_ports:[{node_id:'8',field_name:'image',checked:true}]};
// 同名加载节点必须保留与单次生成相同的完整下游标签；地址值不随显示文案改变。
assert.equal(batchSlotAddressText(s.detail,{node_id:'8',field_name:'image'}),s.detail.params.find(row=>row.node_id==='8').label);
const portsMarkup=batchPortsMarkup(s);
assert.ok(portsMarkup.includes('value="8|image" selected'));
assert.ok(portsMarkup.includes('8 · 载入图片 · image → ref_images.ref_image_0'));
assert.ok(portsMarkup.includes('10 · 载入图片 · image → ref_images.ref_image_1、mask（节点 21）'));
assert.equal(batchSlotAddressText(s.detail,{node_id:'missing',field_name:'image'}),'missing · image');
s.fixed={'9|lora':'other.safetensors'};
// JSON 数组参数：摘要只显示启用数；固定参数区给编辑入口，不再摊开原始字符串。
const loraJson='[{"name":"a","strength_model":1,"strength_clip":1,"enabled":true},{"name":"b","strength_model":0.5,"strength_clip":1,"enabled":false}]';
assert.equal(jsonParamInfo(loraJson).kind,'array');
assert.equal(jsonParamInfo('普通文本'),null);
assert.equal(jsonParamInfo('[1,2]'),null);
assert.equal(jsonParamSummary(loraJson),'1 项启用');
assert.equal(jsonParamButtonText(loraJson),'编辑列表');
assert.equal(jsonParamHelper(loraJson),'本次生成使用编辑后的列表');
assert.equal(jsonParamButtonText('[{"text":"女孩","weight":1.2,"enabled":true}]'),'编辑预设');
assert.equal(jsonParamSummary('[{"name":"a"},{"name":"b"}]'),'2 项');
assert.equal(jsonParamSummary('{"a":1,"b":{"c":2}}'),'2 个字段');
assert.equal(jsonEditableValue('[1]').ok,false);
assert.match(jsonEditableValue('{bad').message,/格式有误/);
const addedEntry=jsonNewEntry([{name:'a',strength_model:1,strength_clip:1,enabled:true,meta:{tag:'x'}}]);
assert.equal(addedEntry.name,'');assert.equal(addedEntry.strength_model,1);assert.equal(addedEntry.enabled,true);
assert.equal(JSON.stringify(addedEntry.meta),JSON.stringify({tag:'x'}),'未知嵌套字段必须保留');
const jsonFixedMarkup=batchFixedMarkup(s);
assert.ok(jsonFixedMarkup.includes('1 项启用'));
assert.ok(jsonFixedMarkup.includes('data-batch-json-edit'));
assert.ok(jsonFixedMarkup.includes('data-json-source="fixed"'));
assert.ok(jsonFixedMarkup.includes('编辑列表'));
assert.ok(!jsonFixedMarkup.includes('"strength_clip":1'),'固定参数区不应再渲染整串 JSON');
const jsonCell=batchJsonParamMarkup(s,0,'extras',loraJson,'20|lora_data','p1');
assert.ok(jsonCell.includes('1 项启用'));
assert.ok(jsonCell.includes('data-json-source="extras"'));
assert.ok(jsonCell.includes('data-json-key="20|lora_data"'));
assert.ok(jsonCell.includes('编辑列表'));
// 三来源写回隔离：逐条只碰当前条目，fixed 只碰 scheme.fixed。
const itemA={name:'A',extras:{}},itemB={name:'B'};
jsonTargetAssign({kind:'item',scheme:{detail:s.detail,fixed:{}},item:itemA,itemIndex:0,source:'extras',key:'20|lora_data'},loraJson);
assert.equal(itemA.extras['20|lora_data'],loraJson);
assert.equal(itemB.extras,undefined);
const fixedTarget={fixed:{}};
jsonTargetAssign({kind:'fixed',scheme:fixedTarget,key:'20|lora_data'},loraJson);
assert.equal(fixedTarget.fixed['20|lora_data'],loraJson);
assert.equal(itemA.name,'A');
// 逐条三种主映射也走同一个写回入口，只动当前条目的对应字段。
const mappedItem={name:'M',prompt:'',duration:'',resolution:''};
jsonTargetAssign({kind:'item',item:mappedItem,source:'prompt'},loraJson);
assert.equal(mappedItem.prompt,loraJson);
jsonTargetAssign({kind:'item',item:mappedItem,source:'duration'},loraJson);
assert.equal(mappedItem.duration,loraJson);
jsonTargetAssign({kind:'item',item:mappedItem,source:'resolution'},loraJson);
assert.equal(mappedItem.resolution,loraJson);
// 主映射指向 JSON text 时，条目表格直接显示摘要入口，不静默跳过。
const jsonMapping={prompt_slot:{node_id:'20',field_name:'lora_data'},duration_slot:{node_id:'20',field_name:'lora_data'},resolution_slot:{node_id:'20',field_name:'lora_data'},extra_points:[],media_ports:[]};
const savedMapping=s.mapping;
s.mapping=jsonMapping;
const jsonRow=batchItemRowMarkup(s,{name:'J',script:'',prompt:loraJson,duration:loraJson,resolution:loraJson,shots:1,files:{},extras:{}},0,[]);
assert.ok(jsonRow.includes('data-json-source="prompt"'));
assert.ok(jsonRow.includes('data-json-source="duration"'));
assert.ok(jsonRow.includes('data-json-source="resolution"'));
assert.ok(jsonRow.includes('1 项启用'));
s.mapping=savedMapping;

// 固定参数展开区：自适应字段块，编辑必须回写 scheme.fixed（旧版没有 input/change 绑定）。
const fixedMarkup=batchFixedMarkup(s);
assert.ok(fixedMarkup.includes('cbm-fixed-field'));
assert.ok(fixedMarkup.includes('data-batch-fixed="9|lora"'));
assert.ok(fixedMarkup.includes('data-batch-fixed="11|cache"'));
assert.ok(fixedMarkup.includes('data-fixed-summary'));
assert.ok(fixedMarkup.includes('other.safetensors'));
batchSetFixedValue(s,{dataset:{batchFixed:'9|lora'},value:'third.safetensors'});
assert.equal(s.fixed['9|lora'],'third.safetensors');
batchSetFixedValue(s,{dataset:{batchFixed:'11|cache'},value:'false'});
assert.equal(s.fixed['11|cache'],false);
batchSetFixedValue(s,{dataset:{batchFixed:'11|cache'},value:'true'});
assert.equal(s.fixed['11|cache'],true);
delete s.fixed['11|cache'];
batchSetFixedValue(s,{dataset:{batchFixed:'12|steps'},value:''});
assert.ok(!('12|steps' in s.fixed),'数字参数清空应还原工作流值，不写 0');
s.items=[{name:'条目 1',script:'剧本A',prompt:'场景A',duration:'8',resolution:'848x480',shots:1,
          files:{'0':{path:'/managed/a.png',name:'a.png',thumb_path:''}},
          extras:{'31|tail':'第二段A'}},
         {name:'条目 2',script:'',prompt:'场景B',duration:'',resolution:'',shots:2,files:{},extras:{}}];
// 执行真实条目渲染，覆盖分辨率枚举和独立索引；仅提交形状测试无法发现作用域错误。
let itemMarkup=batchItemsMarkup(s,0);
assert.ok(itemMarkup.includes('data-batch-item="resolution"'));
assert.ok(itemMarkup.includes('data-batch-action="clear-prompts"'));
assert.ok(itemMarkup.includes('data-batch-item="extras"'));
assert.ok(itemMarkup.includes('data-extra-key="31|tail"'));
assert.ok(itemMarkup.includes('第二段'));
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
assert.deepEqual(scheme.mapping,{prompt_slot:{node_id:'5',field_name:'text'},duration_slot:{node_id:'6',field_name:'length'},resolution_slot:{node_id:'7',field_name:'res'},extra_points:[{id:'bx1',kind:'prompt',slot:{node_id:'31',field_name:'tail'}}],media_ports:[{node_id:'8',field_name:'image',checked:true}]});
assert.deepEqual(scheme.overrides,[{node_id:'9',field_name:'lora',value:'third.safetensors'}]);
const [one,two]=scheme.items;
assert.equal(one.script,'剧本A');
assert.equal(one.prompt,'场景A');assert.equal(one.duration_seconds,8);assert.equal(one.resolution,'848x480');assert.equal(one.shots,1);
// 指认补充值产出在 VM 域里，原型不等，用 JSON 比对形状。
assert.equal(JSON.stringify(one.extras),JSON.stringify({'31|tail':'第二段A'}));
assert.deepEqual(one.media_inputs,[{node_id:'8',field_name:'image',path:'/managed/a.png',media:'image'}]);
assert.equal(two.duration_seconds,null);assert.equal(two.resolution,'');assert.equal(two.media_inputs.length,0);
assert.equal(JSON.stringify(two.extras),'{}');
assert.equal(two.shots,2);
// 换节点/取消勾选后条目文件键按已勾选口序重排：ordinal 语义测试。
const b=batch.draft.schemes[0];
b.mapping.media_ports.push({node_id:'9x',field_name:'audio',checked:false});
assert.equal(b.items[0].files['0'].path,'/managed/a.png');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(["node", "--experimental-vm-modules", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rerun_endpoints_are_routed_and_scoped(self):
        http = (ROOT / "naiba/http.py").read_text(encoding="utf-8")
        service = (ROOT / "naiba/comfyui/service.py").read_text(encoding="utf-8")
        jobs = (ROOT / "naiba/comfyui/jobs.py").read_text(encoding="utf-8")
        self.assertIn('path.endswith("/rerun-snapshot")', http)
        self.assertIn("api_task_rerun_snapshot", http)
        self.assertIn('path.endswith("/rerun")', http)
        self.assertIn("api_task_rerun", http)
        self.assertIn("def rerun_snapshot(", jobs)
        self.assertIn("def submit_rerun(", jobs)
        self.assertIn("只有已完成的单次任务可以再次生成", jobs)
        self.assertIn("批量条目请使用批次里的重试或编辑", jobs)
        self.assertIn("entry_override", jobs)
        self.assertNotIn("checkpoint", service.split("def api_task_rerun_snapshot")[1].split("def ")[0])
        js = (ROOT / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        self.assertIn("dataset.comfyuiRerun", js)
        self.assertIn('rerun.textContent = "再次生成"', js)
        self.assertIn("/rerun-snapshot", js)
        self.assertIn("/rerun", js)
        self.assertIn("data-comfyui-rerun-cancel", js)
        self.assertIn("comfyui-rerun-banner", js)
        self.assertIn("rerunMediaProblems", js)
        self.assertIn("再次生成使用原任务快照，不新增或修改工作流库条目", js)
        self.assertIn("再次生成使用原任务快照，不修改工作流库的显示参数", js)

    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_rerun_completed_task_prefill_submit_and_clear(self):
        # 完成态单次任务：只多一个「再次生成」；快照回填参数/媒体/段数/释放开关；
        # 失效媒体必须先重选或 ⊘；提交走 rerun 接口、成功后清草稿，失败保留。
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/24-comfyui.js','utf8');
(async()=>{
const nodes=new Map(),requests=[],toasts=[],slotValues={};
const makeNode=()=>({value:'',checked:false,disabled:false,hidden:false,open:false,dataset:{},textContent:'',innerHTML:'',options:[],children:[],className:'',classList:{add(){},remove(){},toggle(){},contains(){return false}},setAttribute(name,value){this[name]=String(value);},getAttribute(name){return this[name];},removeAttribute(){},addEventListener(){},append(...items){this.children.push(...items);},closest(){return null},querySelector(){return null},querySelectorAll(){return []},style:{},remove(){}});
const $=key=>{if(!nodes.has(key))nodes.set(key,makeNode());return nodes.get(key)};
const documentStub={
  createElement:()=>makeNode(),
  querySelector:(selector)=>{
    const match=String(selector).match(/data-comfyui-slot="([^"]+)"/);
    if(!match)return null;
    const key=match[1];
    if(key==='8|image'||key==='9|audio')return null;
    const node=makeNode();node.value=slotValues[key]??'';return node;
  },
  querySelectorAll:()=>[],
  addEventListener(){},
  activeElement:null,
  body:{append(){}},
};
let failRerun=false;
const snapshot={source_task_id:'done',workflow_id:'cw-1',workflow_name:'原任务',
  prompt:{'5':{inputs:{text:'原值'}},'6':{inputs:{steps:6}},'8':{inputs:{image:'fixed.png'}},'9':{inputs:{audio:''}}},
  params:[{node_id:'5',field_name:'text',field_type:'text',role:'positive_prompt',control:'multiline',label:'正文'},
          {node_id:'6',field_name:'steps',field_type:'number',default:6,label:'步数'},
          {node_id:'8',field_name:'image',field_type:'image',control:'media',media:'image',default:'fixed.png',media_optional:true,label:'参考图'},
          {node_id:'9',field_name:'audio',field_type:'audio',control:'media',media:'audio',default:'',media_optional:false,label:'音频'}],
  overrides:[{node_id:'5',field_name:'text',value:'覆盖正文'},{node_id:'6',field_name:'steps',value:9}],
  media_inputs:[{node_id:'8',field_name:'image',path:'/managed/a.png',name:'a.png',media:'image',available:false}],
  media_skip:[{node_id:'9',field_name:'audio'}],
  shots:3,unload_after:true,created_at:1000,finished_at:2000};
const context=vm.createContext({console,setTimeout,clearTimeout,setInterval,clearInterval,URL,Date,document:documentStub,window:{addEventListener(){}},localStorage:{setItem(){},getItem:()=>null},CSS:{escape:value=>String(value)}});
const values={$,toast:message=>toasts.push(String(message)),state:{providerModelCatalogs:{},bootstrap:{model_profiles:[],media_exts:{exts:{}}}},localFileUrl:path=>path,attachmentThumbUrl:record=>record.thumb_path||record.path,mediaKind:()=> 'image',saveLocalFile:()=>false,api:async(path,options)=>{
  const method=String(options?.method||'GET').toUpperCase();
  requests.push({path,method,body:options?.body?JSON.parse(JSON.stringify(options.body)):null});
  if(path==='/api/comfyui/tasks/done/rerun-snapshot')return {snapshot};
  if(path==='/api/comfyui/tasks/done/rerun'&&method==='POST'){
    if(failRerun)throw new Error('远端拒绝');
    return {task:{id:'ct-new'}};
  }
  if(path==='/api/comfyui/tasks')return {tasks:[]};
  return {};
}};
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
const m=await make('comfy',source,'\nexport {comfyui,submitTask,renderTaskCard,openRerunSnapshot,buildRerunDetail,rerunMediaProblems,resetRerunState,cancelRerunDraft,mediaSlotStatus};');
const {comfyui,submitTask,renderTaskCard,openRerunSnapshot,rerunMediaProblems,resetRerunState,cancelRerunDraft,mediaSlotStatus}=m;
const actionsOf=(card)=>card.children.find(child=>String(child.className).includes('comfyui-task-actions'));

// 1) 完成态单次任务只有一个「再次生成」；失败仍只有「重试续跑」；批量不出现。
const finished=renderTaskCard({id:'done',status:'completed',workflow_name:'原任务'});
const doneActions=actionsOf(finished);
assert.ok(doneActions.children.some(button=>button.dataset.comfyuiRerun==='done'&&button.textContent==='再次生成'));
assert.ok(!doneActions.children.some(button=>button.textContent==='编辑'||button.textContent==='复制'));
const batchDone=renderTaskCard({id:'b1',status:'completed',batch_id:'cfb-1'});
assert.ok(!actionsOf(batchDone).children.some(button=>button.dataset.comfyuiRerun));
const failed=renderTaskCard({id:'f1',status:'failed'});
assert.ok(actionsOf(failed).children.some(button=>button.dataset.comfyuiRetry==='f1'&&button.textContent==='重试续跑'));
assert.ok(!actionsOf(failed).children.some(button=>button.dataset.comfyuiRerun));

// 2) 快照回填：覆盖值变成当前值、媒体失效/跳过、段数与释放开关。
await openRerunSnapshot('done');
assert.ok(comfyui.rerun);
assert.equal(comfyui.rerun.sourceTaskId,'done');
assert.equal(comfyui.detail.params.find(slot=>slot.node_id==='5').default,'覆盖正文');
assert.equal(comfyui.detail.params.find(slot=>slot.node_id==='6').default,9);
assert.ok(comfyui.detail.visible_params.some(row=>row.node_id==='6'));
assert.equal(comfyui.mediaSelection.get('8|image').available,false);
assert.ok(comfyui.mediaSkipped.has('9|audio'));
assert.equal($('#comfyuiShots').value,'3');
assert.equal($('#comfyuiUnloadAfter').checked,true);
const form=$('#comfyuiParamForm');
const banner=form.children.find(child=>String(child.className).includes('comfyui-rerun-banner'));
assert.ok(banner&&banner.children[0].textContent.includes('来源任务'));
assert.ok(form.children.some(child=>String(child.className).includes('comfyui-rerun-alert')));
assert.equal(mediaSlotStatus(comfyui.detail.params[2],comfyui.mediaSelection.get('8|image'),false,true).badge,'文件失效');
assert.equal($('#comfyuiSaveCurrentAs').hidden,true);
const editButton=form.children.find(child=>String(child.className).includes('comfyui-param-heading-row'))?.children
  .find(child=>String(child.className).includes('comfyui-param-heading-actions'))?.children
  .find(child=>child.textContent==='设置高级参数');
assert.equal(editButton?.style.display,'none');

// 3) 失效媒体未处理：前端拦住提交，不发 rerun 请求。
slotValues['5|text']='用户改正文';slotValues['6|steps']='12';
requests.length=0;toasts.length=0;
await submitTask();
assert.equal(requests.filter(row=>row.method==='POST'&&row.path==='/api/comfyui/tasks/done/rerun').length,0);
assert.ok(toasts.some(message=>message.includes('媒体文件已不存在')));
assert.ok(comfyui.rerun,'失效媒体被拦下时草稿必须保留');

// 4) 重选失效媒体后提交：走 rerun 接口、不带工作流基底；成功后清草稿。
comfyui.rerun.unavailable.delete('8|image');
comfyui.mediaSelection.set('8|image',{path:'/managed/new.png',name:'new.png',thumb_path:'/managed/new.png',available:true});
requests.length=0;
await submitTask();
const post=requests.find(row=>row.method==='POST'&&row.path==='/api/comfyui/tasks/done/rerun');
assert.ok(post,'必须 POST /api/comfyui/tasks/done/rerun');
assert.ok(!('workflow_id' in post.body));
assert.equal(post.body.shots,3);
assert.equal(post.body.unload_after,true);
assert.equal(JSON.stringify(post.body.media_skip),JSON.stringify([{node_id:'9',field_name:'audio'}]));
assert.equal(post.body.media_inputs[0].path,'/managed/new.png');
const values5=post.body.overrides.find(row=>row.node_id==='5');
assert.equal(values5.value,'用户改正文');
assert.equal(comfyui.rerun,null);
assert.equal(comfyui.detail,null);
assert.equal(comfyui.mediaSelection.size,0);
assert.equal($('#comfyuiShots').value,'1');
assert.equal($('#comfyuiUnloadAfter').checked,false);
assert.equal($('#comfyuiSaveCurrentAs').hidden,false);
assert.equal(comfyui.activeTaskId,'ct-new');

// 5) 提交失败：保留草稿、用户值和释放开关。
failRerun=true;
await openRerunSnapshot('done');
comfyui.mediaSkipped.add('8|image');
toasts.length=0;
await submitTask();
assert.ok(toasts.some(message=>message.includes('提交失败')));
assert.ok(comfyui.rerun,'提交失败不得清草稿');
assert.ok(comfyui.detail);
assert.equal($('#comfyuiShots').value,'3');
assert.equal($('#comfyuiUnloadAfter').checked,true);
failRerun=false;

// 6) 取消再次生成：清草稿并回到结果页单次档。
cancelRerunDraft();
assert.equal(comfyui.rerun,null);
assert.equal(comfyui.detail,null);
assert.equal($('#studioPageComfyui').dataset.comfyuiPane,'tasks');
assert.equal($('#studioPageComfyui').dataset.resultsSegment,'single');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(["node", "--experimental-vm-modules", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_single_and_batch_media_skip_and_elapsed_contract(self):
        # 单次与批量同一套三态：原文件非空沿用、空值 optional 自动跳过、⊘ 手动
        # 强制跳过（最高优先级，不受必填声明限制）；耗时只用持久时间。
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/24-comfyui.js','utf8');
(async()=>{
const nodes=new Map(),requests=[],toasts=[];
const makeNode=()=>({value:'',checked:false,disabled:false,hidden:false,open:false,dataset:{},textContent:'',innerHTML:'',options:[],children:[],className:'',classList:{add(){},remove(){},toggle(){},contains(){return false}},setAttribute(name,value){this[name]=String(value);},removeAttribute(){},addEventListener(){},append(...items){this.children.push(...items);},closest(){return null},querySelector(){return null},querySelectorAll(){return []},style:{}});
const $=key=>{if(!nodes.has(key))nodes.set(key,makeNode());return nodes.get(key)};
const context=vm.createContext({console,setTimeout,clearTimeout,setInterval,clearInterval,URL,Date,document:{createElement:()=>makeNode(),querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},activeElement:null,body:{append(){}},addEventListener(){}},window:{addEventListener(){}},localStorage:{setItem(){},getItem:()=>null},CSS:{escape:value=>String(value)}});
const values={$,toast:message=>toasts.push(String(message)),state:{providerModelCatalogs:{},bootstrap:{model_profiles:[]}},localFileUrl:path=>path,attachmentThumbUrl:record=>record.path,mediaKind:()=> 'image',saveLocalFile:()=>false,api:async(path,options)=>{
  const method=String(options?.method||'GET').toUpperCase();
  requests.push({path,method,body:options?.body?JSON.parse(JSON.stringify(options.body)):null});
  if(path==='/api/comfyui/tasks'&&method==='POST')return {task:{id:'ct-single'}};
  if(path==='/api/comfyui/batches'&&method==='POST')return {batch:{id:'cfb-x2'}};
  if(path==='/api/comfyui/tasks')return {tasks:[]};
  return {};
}};
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
const m=await make('comfy',source,'\nexport {comfyui,submitTask,mediaSlotStatus,collectMediaInputs,collectMediaSkips,mediaSkipButton,renderMediaTile,batch,ensureBatchDraft,submitBatch,batchItemsMarkup,batchSchemeFromRestore,batchElapsedText};');
const {comfyui,submitTask,mediaSlotStatus,collectMediaInputs,collectMediaSkips,mediaSkipButton,renderMediaTile,batch,ensureBatchDraft,submitBatch,batchItemsMarkup,batchSchemeFromRestore,batchElapsedText}=m;

// ---- 单次媒体槽 ----
comfyui.detail={id:'cw-1',name:'单次流',prompt:{},params:[
  {node_id:'8',field_name:'image',field_type:'image',control:'media',media:'image',default:'fixed.png',media_optional:true,label:'8 · 固定图'},
  {node_id:'9',field_name:'image',field_type:'image',control:'media',media:'image',default:'',media_optional:true,label:'9 · 可选图'},
  {node_id:'10',field_name:'audio',field_type:'audio',control:'media',media:'audio',default:'',media_optional:false,label:'10 · 必填音'},
]};
assert.equal(mediaSlotStatus(comfyui.detail.params[0],null).badge,'沿用原文件');
assert.equal(mediaSlotStatus(comfyui.detail.params[1],null).badge,'未选即跳过');
assert.equal(mediaSlotStatus(comfyui.detail.params[2],null).badge,'请选择文件');
assert.equal(mediaSlotStatus(comfyui.detail.params[0],null,true).badge,'已跳过');
// ⊘ 与已选文件同时存在：跳过优先，两个槽都不进 media_inputs。
comfyui.mediaSelection.set('8|image',{path:'/managed/a.png',name:'a.png'});
comfyui.mediaSkipped.add('8|image');
comfyui.mediaSkipped.add('10|audio');
assert.equal(JSON.stringify(collectMediaInputs()),'[]');
assert.equal(JSON.stringify(collectMediaSkips()),JSON.stringify([
  {node_id:'8',field_name:'image'},{node_id:'10',field_name:'audio'}]));
await submitTask();
const single=requests.find(row=>row.path==='/api/comfyui/tasks'&&row.method==='POST');
assert.ok(single,'必须 POST /api/comfyui/tasks');
assert.equal(JSON.stringify(single.body.media_inputs),'[]');
assert.equal(JSON.stringify(single.body.media_skip),JSON.stringify([
  {node_id:'8',field_name:'image'},{node_id:'10',field_name:'audio'}]));
// 跳过块有可访问名称，再次点击即取消（状态恢复）。
const skipButton=mediaSkipButton('8|image',true);
assert.equal(skipButton.textContent,'⊘');
assert.equal(skipButton['aria-label'],'取消跳过，恢复此前状态');
const tile=renderMediaTile(comfyui.detail.params[0],'8|image','image');
assert.equal(tile.dataset.comfyuiMediaSkipped,'1');
assert.ok(String(tile.className).includes('skipped'));
assert.ok(tile.children.some(child=>child.textContent==='⊘'));

// ---- 批量条目：沿用 / 自动跳过 / 手动 ⊘ ----
ensureBatchDraft();
const s=batch.draft.schemes[0];
s.workflow_id='cw-2';
s.detail={id:'cw-2',name:'批量流',workflow_fingerprint:'fp-2',prompt:{},params:[
  {node_id:'8',field_name:'image',field_type:'image',control:'media',media:'image',default:'fixed.png',media_optional:true,label:'8 · 固定图'},
  {node_id:'9',field_name:'image',field_type:'image',control:'media',media:'image',default:'',media_optional:true,label:'9 · 可选图'},
  {node_id:'10',field_name:'image',field_type:'image',control:'media',media:'image',default:'',media_optional:false,label:'10 · 必填图'},
]};
s.mapping={prompt_slot:null,duration_slot:null,resolution_slot:null,extra_points:[],
  media_ports:[{node_id:'8',field_name:'image',checked:true},
               {node_id:'9',field_name:'image',checked:true},
               {node_id:'10',field_name:'image',checked:true}]};
s.items=[
  {name:'条目 A',script:'',prompt:'场景A',duration:'',resolution:'',shots:1,extras:{},files:{},skips:{'10|image':true}},
  {name:'条目 B',script:'',prompt:'场景B',duration:'',resolution:'',shots:1,extras:{},files:{'2':{path:'/managed/b.png',name:'b.png'}},skips:{}},
];
const markup=batchItemsMarkup(s,0);
assert.ok(markup.includes('沿用原文件'));
assert.ok(markup.includes('留空 · 自动跳过'));
assert.ok(markup.includes('已跳过这个输入'));
assert.ok(markup.includes('cbm-tile-skip active'));
assert.ok(markup.includes('data-batch-action="toggle-skip"'));
// 必填口没有文件，但手动 ⊘ 了：前端不能把它标成缺项。
batch.recheckMissing();
assert.equal(batch.missingItems.size,0,'手动跳过应绕过必填检查');
batch.draft.title='跳过验收';batch.draft.unload_after=false;
await submitBatch();
const post=requests.find(row=>row.path==='/api/comfyui/batches');
assert.ok(post,'必须 POST /api/comfyui/batches');
const [a,b]=post.body.schemes[0].items;
assert.equal(JSON.stringify(a.media_inputs),'[]');
assert.equal(JSON.stringify(a.media_skip),JSON.stringify([{node_id:'10',field_name:'image'}]));
assert.equal(JSON.stringify(b.media_skip),'[]');
assert.equal(b.media_inputs.length,1);
// 快照回填：media_skip 按地址放回条目。
const restored=batchSchemeFromRestore({workflow_id:'cw-2',workflow_name:'批量流',overrides:[],
  mapping:s.mapping,
  items:[{name:'条目 A',prompt:'场景A',shots:1,extras:{},
          media_inputs:[],media_skip:[{node_id:'10',field_name:'image'}]}]},s.detail);
assert.equal(restored.items[0].skips['10|image'],true);

// ---- 耗时：完成态固定，缺时间显示未知 ----
assert.equal(batchElapsedText({active:true,started_at:1000},61000),'已用 01:00');
assert.equal(batchElapsedText({active:false,started_at:1000,finished_at:31000},99999999),'耗时 00:30');
assert.equal(batchElapsedText({active:false,started_at:1000,finished_at:31000},1),'耗时 00:30');
assert.equal(batchElapsedText({active:false},0),'耗时未知');
assert.equal(batchElapsedText({active:true},0),'等待开始');
assert.equal(batchElapsedText({active:false,started_at:1000},99999999),'耗时未知');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(["node", "--experimental-vm-modules", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node required for frontend logic")
    def test_single_submission_unload_after_switch_contract(self):
        # 单次「释放模型」开关契约：请求显式携带 unload_after；任务受理成功后
        # 开关自动复位；提交失败保留勾选；批量草稿的开关不影响单次请求。
        # 批量自身读 draft.unload_after 的契约由
        # test_batch_submission_shape_mapping_resolution_and_convert 钉住。
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/24-comfyui.js','utf8');
(async()=>{
const nodes=new Map(),requests=[],toasts=[];
const makeNode=()=>({value:'',checked:false,disabled:false,hidden:false,open:false,dataset:{},textContent:'',innerHTML:'',options:[],children:[],classList:{add(){},remove(){},toggle(){},contains(){return false}},setAttribute(){},removeAttribute(){},addEventListener(){},append(){},closest(){return null},querySelector(){return null},querySelectorAll(){return []},style:{}});
const $=key=>{if(!nodes.has(key))nodes.set(key,makeNode());return nodes.get(key)};
let failSubmit=false;
const context=vm.createContext({console,setTimeout,clearTimeout,setInterval,clearInterval,URL,document:{createElement:()=>makeNode(),querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},activeElement:null,body:{append(){}},addEventListener(){}},window:{addEventListener(){}},localStorage:{setItem(){},getItem:()=>null},CSS:{escape:value=>String(value)}});
const values={$,toast:message=>toasts.push(String(message)),state:{providerModelCatalogs:{},bootstrap:{model_profiles:[]}},api:async(path,options)=>{
  const method=String(options?.method||'GET').toUpperCase();
  requests.push({path,method,body:options?.body?JSON.parse(JSON.stringify(options.body)):null});
  if(path==='/api/comfyui/tasks'&&method==='POST'){
    if(failSubmit)throw new Error('远端拒绝');
    return {task:{id:'cft-1'}};
  }
  return {tasks:[]};
}};
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
const m=await make('comfy',source,'\nexport {comfyui,submitTask,batch,ensureBatchDraft};');
const {comfyui,submitTask,batch,ensureBatchDraft}=m;
comfyui.detail={id:'cw-1',name:'测试流',params:[]};
const shots=$('#comfyuiShots');shots.value='2';
const toggle=$('#comfyuiUnloadAfter');
const postTask=()=>requests.find(row=>row.path==='/api/comfyui/tasks'&&row.method==='POST');

// 1) 未勾选：请求显式带 false；受理成功后保持未勾选。
await submitTask();
let post=postTask();
assert.ok(post,'必须 POST /api/comfyui/tasks');
assert.equal(post.body.unload_after,false);
assert.equal(post.body.shots,2);
assert.equal(post.body.workflow_id,'cw-1');
assert.equal(toggle.checked,false);
assert.equal(comfyui.activeTaskId,'cft-1');

// 2) 勾选提交：请求带 true；任务受理成功后开关自动复位。
toggle.checked=true;requests.length=0;
await submitTask();
post=postTask();
assert.equal(post.body.unload_after,true);
assert.equal(toggle.checked,false,'任务受理成功后开关自动复位');

// 3) 提交失败：请求仍带勾选值，失败不清除勾选（用户重试不丢选择）。
toggle.checked=true;requests.length=0;failSubmit=true;
await submitTask();
post=postTask();
assert.equal(post.body.unload_after,true);
assert.equal(toggle.checked,true,'提交失败不得清除勾选');
assert.ok(toasts.some(row=>row.includes('提交失败')));
failSubmit=false;

// 4) 批量隔离：批量草稿开着释放，单次请求仍取开关自身状态。
ensureBatchDraft();batch.draft.unload_after=true;
toggle.checked=false;requests.length=0;
await submitTask();
post=postTask();
assert.equal(post.body.unload_after,false,'单次请求不读批量草稿的释放开关');
assert.equal(toggle.checked,false,'成功后复位，不受批量草稿影响');
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
                     ".comfyui-batch-row-wrap", ".comfyui-batch-row-edit", ".cbm-btn.danger",
                     ".cbm-th-clear", ".cbm-card-preview", ".cbm-preview-play",
                     ".cbm-back-to-list", ".comfyui-batch-detail", ".cbm-tile-skip"):
            self.assertIn(rule, css)
        self.assertIn('[data-results-segment="single"] .comfyui-batch-history', css)
        self.assertIn('[data-results-segment="batch"] .comfyui-tasks', css)
        self.assertIn('[data-results-segment="batch"] .comfyui-outputs', css)
        self.assertIn('[data-batch-detail="1"] .comfyui-batch-history', css)
        # 跑批视图：排队/生成中的条目卡有取消；完成条目的「打开文件夹」定位本条产物目录。
        self.assertIn('data-batch-action="cancel-item"', js)
        self.assertIn('data-batch-action="reveal-item-folder"', js)
        self.assertIn("function revealBatchItemFolder(", js)
        self.assertIn('data-batch-action="back-to-batch-list"', js)
        self.assertIn("function renderResultsBatchDetail(", js)
        self.assertIn("function syncResultsBatchDetail(", js)
        self.assertIn("function batchRunVisible(", js)
        self.assertIn("data-batch-elapsed", js)
        self.assertIn("耗时未知", js)
        self.assertNotIn('data-batch-action="toggle-item-outputs"', js)
        self.assertNotIn("function mountBatchItemOutputs()", js)
        self.assertNotIn('data-batch-action="open-task-outputs"', js)
        # 批量完成态与单次结果共用同一张产物卡：视频能播、竖屏按比例显示。
        self.assertIn("function mountBatchItemPreviews()", js)
        self.assertIn("data-batch-preview-task", js)
        self.assertIn('batchHost.addEventListener("click", onOutputGridClick)', js)
        self.assertIn('for (const selector of ["#comfyuiBatchBody", "#comfyuiBatchDetail"])', js)
        self.assertIn("function batchRunSignature(", js)
        # 历史批次：能删产物、删批次，也能回填填单区再跑。
        for action in ("edit-batch", "clear-batch-outputs", "delete-batch", "delete-item"):
            self.assertIn(f'data-batch-action="{action}"', js)
        self.assertIn('data-batch-action="open-history"', js)
        # 单次档的「清除」不碰批量任务：批量产物按批次删。
        self.assertIn("exclude_batch: true", js)
        # 转换模板的默认档是自动：普通参考图走参考六字段，首尾帧走基础三字段。
        self.assertIn("自动（按工作流素材槽位选基础/参考）", js)
        self.assertIn("BATCH_H3_BASE_TEMPLATE", js)
        convert_js = (ROOT / "public/js/24a-comfyui-convert.js").read_text(encoding="utf-8")
        self.assertIn("export const BATCH_H3_BASE_TEMPLATE", convert_js)
        self.assertIn("function evidenceSaysReference(", convert_js)
        # 提词库内置模板也要出现在转换模板下拉：按模块分组，值带 catalog: 前缀。
        self.assertIn("/api/studio/catalog/items?type=template", js)
        self.assertIn("BATCH_TEMPLATE_MODULES", js)
        self.assertIn("`catalog:${item.id}`", js)
        self.assertIn('template.startsWith("catalog:")', convert_js)
        # ＋增加指认：点加号才出现指认行；四类写入位按入列逐条控制，备用槽认任何可改参数。
        for marker in ("function extraPointsOf(", "function extraSlotPool(", "function promptSlotPool(",
                       'data-batch-action="add-extra"', 'data-batch-action="remove-extra"',
                       "data-batch-extra-kind", "data-batch-extra-slot", 'data-batch-item="extras"',
                       "function batchExtraCellMarkup(", "function itemExtraValue("):
            self.assertIn(marker, js)
        self.assertIn("function splitBatchPromptSegments(", js)
        for rule in (".cbm-extras", ".cbm-extra-row", ".cbm-extra-kind", ".cbm-extra-slot",
                     "td[data-batch-extra]", ".cbm-extra-prompt"):
            self.assertIn(rule, css)
        # 转换带图小字提醒：每条显示这次转换真正发给视觉模型的图片数。
        self.assertIn("function batchItemConversionImageCount(", js)
        self.assertIn('class="cbm-convert-hint', js)
        # 第二列转换：带第二段写入位时双段输出协议随请求发出，模型按 ---- 分段。
        self.assertIn("输出协议：这份工作流要把提示词分两处注入", convert_js)
        self.assertIn("prompt2Point", convert_js)
        # 结果页产物卡的视频封面：进入视野才读首帧，三条挂载点全接同一个升级观察器。
        self.assertIn("batchPreviewVideoPreload(existing.get(key))", js)
        self.assertIn("batchPreviewVideoPreload(host);", js)
        # 拆书匠分镜交接落到批量档：「从分镜队列导入」直接可见；单次档不再接分镜队列。
        handoff = js[js.index("function onStoryboardHandoff("):js.index("const batch = {")]
        self.assertIn('setComfyuiPane("batch");', handoff)
        self.assertNotIn("comfyuiStoryboard", js)
        self.assertNotIn("renderStoryboardQueue", js)
        self.assertNotIn("fillStoryboardPrompt", js)
        # 空行不提交：导入清默认空行，提交前再过滤，自检也不为空行标红。
        self.assertIn("function batchItemIsBlank(", js)
        self.assertIn("scheme.items = scheme.items.filter((item) => !batchItemIsBlank(item));", js)
        self.assertIn("const readyItems = (scheme.items || []).filter((item) => !batchItemIsBlank(item));", js)
        self.assertIn("if (batchItemIsBlank(item)) return;", js)
        self.assertIn("data-batch-edit", js)
        # 媒体槽三态：沿用原文件 / 已选新文件 / 手动 ⊘ 强制跳过（单次与批量同口径）。
        self.assertIn("mediaSkipped: new Set()", js)
        self.assertIn("function mediaSkipButton(", js)
        self.assertIn("function collectMediaSkips(", js)
        self.assertIn("media_skip: collectMediaSkips()", js)
        self.assertIn('data-batch-action="toggle-skip"', js)
        self.assertIn("function itemSkipsPort(", js)
        self.assertIn("media_skip: mediaSkip", js)
        self.assertIn("mediaSlotOriginalNonEmpty(", js)
        self.assertIn("function batchSchemeFromRestore(", js)
        self.assertIn("function batchSchemeFromLegacy(", js)
        self.assertIn('with_schemes=1', js)
        self.assertNotIn('data-batch-action="switch-draft"', js)

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
const m=await make('comfy',source,'\nexport {batch,batchHistoryBadge,renderBatchHistoryList,batchRunCardMarkup,batchRunMarkup,batchRunSignature,batchSchemeFromRestore,batchSchemeFromLegacy,isBatchTaskRow,batchItemIsBlank};');
const {batch,batchHistoryBadge,renderBatchHistoryList,batchRunCardMarkup,batchRunMarkup,batchRunSignature,batchSchemeFromRestore,batchSchemeFromLegacy,isBatchTaskRow,batchItemIsBlank}=m;
// 空行口径：默认名 + 什么都没填才算空；改过名、填过字、挂过素材、调过条数都不算。
assert.equal(batchItemIsBlank({name:'条目 1',script:'',prompt:'',duration:'',resolution:'',shots:1,files:{}}),true);
assert.equal(batchItemIsBlank({name:'条目 2',prompt:'提示词',shots:1,files:{}}),false);
assert.equal(batchItemIsBlank({name:'条目 3',files:{'0':{path:'uploads/a.png'}}}),false);
assert.equal(batchItemIsBlank({name:'我的镜头',shots:1,files:{}}),false);
assert.equal(batchItemIsBlank({name:'条目 4',shots:2}),false);
assert.equal(batchItemIsBlank({name:'条目 5',duration:'6'}),false);
// 指认列里填过任何补充值也不算空行；空串不算填过。
assert.equal(batchItemIsBlank({name:'条目 6',shots:1,files:{},extras:{'31|tail':'补充'}}),false);
assert.equal(batchItemIsBlank({name:'条目 7',shots:1,files:{},extras:{'31|tail':''}}),true);
assert.equal(isBatchTaskRow({batch_id:'cfb-a1'}),true);
assert.equal(isBatchTaskRow({batch_id:''}),false);
assert.equal(isBatchTaskRow({}),false);
assert.equal(isBatchTaskRow(null),false);
// 徽标数组跨 VM 域比较原型不等，用 JSON 字符串比对形状。
assert.equal(JSON.stringify(batchHistoryBadge({items:[{status:'running'},{status:'completed'}],done_count:1,item_count:2})),JSON.stringify(['run','正在生成 1/2']));
assert.equal(JSON.stringify(batchHistoryBadge({items:[{status:'completed'},{status:'completed'}],done_count:2,item_count:2})),JSON.stringify(['done','2 条全部完成']));
assert.equal(JSON.stringify(batchHistoryBadge({items:[{status:'failed'},{status:'completed'},{status:'cancelled'}],done_count:1,item_count:3})),JSON.stringify(['fail','1/3 完成 · 2 失败']));
// 任务记录被删不算失败，单独报「产物已删」。
assert.equal(JSON.stringify(batchHistoryBadge({items:[{status:'missing'},{status:'missing'}],done_count:0,item_count:2})),JSON.stringify(['queue','0/2 完成 · 2 条产物已删']));
batch.histories=[
  {id:'cfb-a0',title:'短剧第2集',done_count:3,item_count:6,created_at:0,items:[{status:'running'},{status:'completed'},{status:'completed'}]},
  {id:'cfb-a1',title:'短剧第1集',done_count:6,item_count:6,created_at:new Date('2026-11-06T13:05:00').getTime(),items:[{status:'completed'}]},
  {id:'cfb-a2',title:'短剧第3集',done_count:2,item_count:6,created_at:0,items:[{status:'failed'},{status:'failed'},{status:'completed'}]}];
renderBatchHistoryList();
const html=$('#comfyuiBatchList').innerHTML;
assert.ok(html.includes('data-batch-open="cfb-a1"')&&html.includes('短剧第1集'));
assert.ok(html.includes('data-batch-edit="cfb-a1"')&&html.includes('回到填单区修改这一批'));
assert.ok(html.includes('cbm-badge done')&&html.includes('6 条全部完成'));
assert.ok(html.includes('is-failed')&&html.includes('cbm-badge fail'));
assert.ok(html.includes('正在生成'));
batch.histories=[];
renderBatchHistoryList();
assert.ok($('#comfyuiBatchList').innerHTML.includes('还没有批次'));
const running=batchRunCardMarkup({index:1,name:'条目 1',status:'running',done:1,total:3,task_id:'ct-9',outputs:[]});
assert.ok(running.includes('data-batch-action="cancel-item"')&&running.includes('data-task-id="ct-9"'));
assert.ok(!running.includes('open-task-outputs'));
const done=batchRunCardMarkup({index:2,name:'条目 2',status:'completed',done:2,total:2,task_id:'ct-10',outputs:['a.png','b.png']});
assert.ok(done.includes('data-batch-action="reveal-item-folder"'));
assert.ok(done.includes('data-batch-action="delete-item"'));
assert.ok(!done.includes('data-batch-expand-for'));
// 完成态预览挂单次结果同款产物卡，不再自己画缩略图（视频/竖屏都交给产物卡）。
assert.ok(done.includes('data-batch-preview-task="ct-10"')&&done.includes('data-batch-preview-name="a.png"'));
assert.ok(!done.includes('cbm-card-thumb'));
// 轮询只在条目状态变化时重绘，别打断正在播放的视频。
assert.equal(batchRunSignature({items:[{task_id:'ct-1',status:'completed',outputs:['a.png']}]}),
             batchRunSignature({items:[{task_id:'ct-1',status:'completed',outputs:['a.png']}]}));
assert.notEqual(batchRunSignature({items:[{task_id:'ct-1',status:'running'}]}),
                batchRunSignature({items:[{task_id:'ct-1',status:'completed'}]}));
const failed=batchRunCardMarkup({index:3,name:'条目 3',status:'failed',error:'超时',task_id:'ct-11',outputs:[]});
assert.ok(failed.includes('data-batch-action="retry-item"')&&!failed.includes('cancel-item'));
assert.ok(failed.includes('data-batch-action="delete-item"'));
// 任务记录已删：不再给必然 404 的重试，徽标改中性文案。
const missing=batchRunCardMarkup({index:4,name:'条目 4',status:'missing',error:'任务记录和产物已删除',task_id:'ct-12',outputs:[]});
assert.ok(missing.includes('产物已删除')&&!missing.includes('retry-item')&&!missing.includes('delete-item'));
// 跑批头：编辑、清产物、删批次三个入口都在。
batch.runData={title:'短剧第1集',items:[
  {index:1,name:'条目 1',status:'completed',task_id:'ct-1',outputs:['a.png']},
  {index:2,name:'条目 2',status:'missing',task_id:'ct-2',outputs:[]},
]};
const runHead=batchRunMarkup();
assert.ok(runHead.includes('data-batch-action="edit-batch"'));
assert.ok(runHead.includes('data-batch-action="clear-batch-outputs"'));
assert.ok(runHead.includes('data-batch-action="delete-batch"'));
assert.ok(runHead.includes('1/2 完成 · 1 条产物已删'));
// 新批次快照回填：映射、固定参数、条目、素材文件都按原样放回。
const detail={id:'cw-1',name:'测试流',workflow_fingerprint:'fp-x',
  prompt:{'5':{inputs:{text:'默认正文'}},'6':{inputs:{length:5}},'7':{inputs:{res:'848x480'}},'8':{inputs:{image:'old.png'}},'9':{inputs:{lora:'base.safetensors'}}},
  params:[{node_id:'5',field_name:'text',field_type:'text',control:'multiline',role:'positive_prompt',label:'正文'},
          {node_id:'6',field_name:'length',field_type:'number',default:5,label:'时长'},
          {node_id:'7',field_name:'res',field_type:'text',candidates:['848x480','480x848'],label:'分辨率'},
          {node_id:'8',field_name:'image',field_type:'image',control:'media',media:'image',label:'参考图'},
          {node_id:'9',field_name:'lora',field_type:'text',role:'lora',label:'LoRA'}]};
const restored=batchSchemeFromRestore({
  workflow_id:'cw-1',workflow_name:'测试流',
  overrides:[{node_id:'9',field_name:'lora',value:'other.safetensors'}],
  mapping:{prompt_slot:{node_id:'5',field_name:'text'},duration_slot:{node_id:'6',field_name:'length'},
           resolution_slot:{node_id:'7',field_name:'res'},
           extra_points:[{id:'bx2',kind:'spare',slot:{node_id:'9',field_name:'lora'}},
                         {id:'bx3',kind:'prompt',slot:{node_id:'99',field_name:'gone'}}],
           media_ports:[{node_id:'8',field_name:'image',checked:true}]},
  items:[{name:'条目 1',script:'剧本A',prompt:'场景A',duration_seconds:8,resolution:'848x480',shots:2,
          extras:{'9|lora':'帽型 v2','99|ghost':'不该回来的值'},
          media_inputs:[{node_id:'8',field_name:'image',path:'/managed/a.png',media:'image',name:'a.png'}]}],
},detail);
assert.equal(restored.mapping.prompt_slot.node_id,'5');
// 指认行原样回填：槽位还在的保留，槽位没了的整行丢掉并条相应清值。
assert.equal(restored.mapping.extra_points.length,1);
assert.equal(restored.mapping.extra_points[0].kind,'spare');
assert.equal(restored.mapping.extra_points[0].id,'bx2');
assert.equal(JSON.stringify(restored.items[0].extras),JSON.stringify({'9|lora':'帽型 v2'}));
assert.ok(restored.restoreWarning);
assert.equal(restored.items[0].script,'剧本A');
assert.equal(restored.items[0].prompt,'场景A');
assert.equal(restored.items[0].duration,'8');
assert.equal(restored.items[0].resolution,'848x480');
assert.equal(restored.items[0].shots,2);
assert.equal(restored.items[0].files['0'].path,'/managed/a.png');
assert.equal(restored.items[0].files['0'].name,'a.png');
assert.equal(restored.fixed['9|lora'],'other.safetensors');
assert.equal(restored.mapping.media_ports.filter(port=>port.checked).length,1);
// 旧批次：从任务输入捞回能对上的字段，映射走工作流预选。
const legacy=batchSchemeFromLegacy({workflowId:'cw-1',rows:[{row:{name:'条目 1'},restore:{
  overrides:[{node_id:'5',field_name:'text',value:'旧正文'},{node_id:'6',field_name:'length',value:8},
             {node_id:'9',field_name:'lora',value:'base.safetensors'}],
  media_inputs:[{node_id:'8',field_name:'image',path:'/managed/old.png',media:'image',name:'old.png'}],
  shots:2,
}}]},detail);
assert.equal(legacy.items[0].prompt,'旧正文');
assert.equal(legacy.items[0].duration,'8');
assert.equal(legacy.items[0].files['0'].path,'/managed/old.png');
assert.equal(legacy.fixed['9|lora'],'base.safetensors');
assert.ok(legacy.restoreWarning.includes('旧版'));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(["node", "--experimental-vm-modules", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
