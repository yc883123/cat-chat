"""造梦机 JSON 参数弹窗：LoRA 枚举、滚轮调数值、条目紧凑度（2026-11-10 反馈）。

用户口径：
- lora_data（Visual LoRA Loader）里的「模型文件」要能选，不要只让手输；
  候选来自 models/loras 的文件清单，走原生 datalist（模态弹窗在 top layer，
  append 到 body 的自绘下拉会被盖住，实测 elementFromPoint 打不到）。
- 数字字段支持鼠标滚轮：默认 ±0.1，按住 Shift ±0.01，值收口到两位小数。
- 换了模型文件就把同一条目的 sha256 清空（候选清单只有文件名，旧哈希对不上）。
- 条目高度压到 143→82（两字段）/ 213→129（四字段），一屏能多看几条。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function el(tag='div'){
  const e={tagName:tag,children:[],className:'',value:'',dataset:{},style:{},
    hidden:false,checked:false,list:null,
    classList:{add(){},remove(){},toggle(){},contains(){return false}},
    append(...k){for(const kid of k){kid.__attached=true;e.children.push(kid);}},
    replaceChildren(...k){for(const kid of k)kid.__attached=true;e.children=[...k];},
    remove(){e.__attached=false;},handlers:{},
    addEventListener(type,fn){e.handlers[type]=fn;},querySelector(){return null;},closest(){return null;},
    getBoundingClientRect(){return{left:0,top:0,right:0,bottom:0,width:0,height:0};}};
  let text='';
  Object.defineProperty(e,'textContent',{get:()=>text,set(v){text=String(v);if(text==='')e.children.length=0;}});
  Object.defineProperty(e,'isConnected',{get:()=>e.__attached===true});
  return e;
}
const context=vm.createContext({
  console,$:()=>null,$$:()=>[],api:async()=>({}),state:{},toast(){},
  localFileUrl:v=>v,attachmentThumbUrl:v=>v,collectMediaPreviewItems:()=>[],
  isPywebview:()=>false,mediaKind:()=>'',saveLocalFile(){},
  currentStudioPage:()=>'',isStudioOpen:()=>false,onStudioPageChange(){},setStudioPage(){},
  BATCH_H3_BASE_TEMPLATE:'',BATCH_H3_REFERENCE_TEMPLATE:'',buildBatchConvertRequest:()=>({}),
  document:{addEventListener(){},createElement:el,querySelector:()=>null,body:el()},
  window:{addEventListener(){},removeEventListener(){},matchMedia:()=>({matches:false}),innerHeight:812},
  setTimeout,clearTimeout,setInterval,clearInterval,Event,
  CSS:{escape:v=>v},IntersectionObserver:class{observe(){}disconnect(){}},
});
const source=fs.readFileSync('public/js/24-comfyui.js','utf8')
  .replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
vm.runInContext(source+`\nglobalThis.p={jsonEditorRowsAreLora,jsonEditorEntryMarkup,
  jsonEditorEntryWheel,jsonEditorEntryChanged,attachCandidateCombo,comfyJsonEditor,
  isImageContainerSlot,imageContainerRows,imageContainerReferences,imageContainerValueError,
  imageContainerManagedPath,checkImageContainerFiles,imageContainerMissingPaths,defaultVisibleKeys};`,context);
const p=context.p;
const loraRows=[{name:'moss/x.safetensors',strength_model:1,strength_clip:1,sha256:'abc',enabled:true}];
const presetRows=[{text:'女孩',weight:1.6,enabled:true}];
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行造梦机前端")
class JsonEditorLoraEnumTests(unittest.TestCase):
    def run_case(self, body: str) -> None:
        result = subprocess.run([shutil.which("node"), "-"], input=HARNESS + body,
                                cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_lora_rows_detected_by_shape(self):
        self.run_case(r"""
assert.equal(p.jsonEditorRowsAreLora(loraRows),true);
assert.equal(p.jsonEditorRowsAreLora(presetRows),false,'提示词预设不该被当成 LoRA 列表');
assert.equal(p.jsonEditorRowsAreLora([]),false);
assert.equal(p.jsonEditorRowsAreLora([{name:'x'}]),false,'只有 name 不算 LoRA 列表');
""")

    def test_name_field_gets_datalist_only_for_lora_lists(self):
        self.run_case(r"""
const markup=p.jsonEditorEntryMarkup(loraRows[0],0,loraRows);
assert.match(markup,/data-json-field="name"/);
assert.match(markup,/class="comfyui-json-lora-name"/,'可选的模型文件要带下拉箭头提示');
assert.doesNotMatch(markup,/data-json-field="sha256"/,'sha256 是节点内部摘要，左侧不给输入框');
assert.doesNotMatch(markup,/list="comfyuiJsonLoraNames"/,'原生 datalist 会按现值过滤，已换成弹窗内候选下拉');
const preset=p.jsonEditorEntryMarkup(presetRows[0],0,presetRows);
assert.doesNotMatch(preset,/comfyuiJsonLoraNames/);
""")

    def test_combo_inside_dialog_lists_all_candidates_and_notifies(self):
        """模态弹窗里的候选下拉：挂进 dialog、聚焦即列全部、选中回写并发 input。"""
        self.run_case(r"""
const input={value:'moss_image/lodestones/custom.safetensors',handlers:{},lastEvent:null,
  addEventListener(t,f){input.handlers[t]=f;},dispatchEvent(e){input.lastEvent=e;},
  getBoundingClientRect:()=>({left:120,top:300,bottom:326,width:260,height:26})};
const container=el('dialog');container.clientHeight=600;
container.getBoundingClientRect=()=>({left:100,top:100,width:800,height:600});
p.attachCandidateCombo(input,null,()=>['Kook_Zimage_瑶光.safetensors','moss_image/lodestones/custom.safetensors'],
  {container,notify:true});
input.handlers.focus();
const list=container.children[0];
assert.ok(list,'下拉必须挂进弹窗容器（挂 body 会被 top layer 盖住）');
assert.equal(list.hidden,false);
assert.match(list.className,/comfyui-combo-list-inset/);
const texts=list.children.map((c)=>c.textContent);
assert.deepEqual(texts,['Kook_Zimage_瑶光.safetensors','moss_image/lodestones/custom.safetensors'],
  '聚焦要列全部候选，不能按输入框里的现值过滤');
const pick=list.children[0];
pick.handlers.mousedown({preventDefault(){}});
assert.equal(input.value,'Kook_Zimage_瑶光.safetensors');
assert.equal(input.lastEvent?.type,'change','选中后要发 change，让 JSON rows/原始文本跟上并清旧哈希');
assert.equal(list.hidden,true);
""")

    def test_wheel_steps_and_rounding(self):
        self.run_case(r"""
const input={value:'1',dispatched:0,dispatchEvent(){this.dispatched+=1;}};
const ev=(deltaY,shiftKey=false)=>({target:{closest:()=>input},deltaY,shiftKey,
  prevented:0,preventDefault(){this.prevented+=1;}});
let e=ev(-100);p.jsonEditorEntryWheel(e);
assert.equal(input.value,'1.1');assert.equal(e.prevented,1,'必须挡住列表跟着滚');
e=ev(-100,true);p.jsonEditorEntryWheel(e);
assert.equal(input.value,'1.11');
e=ev(100);p.jsonEditorEntryWheel(e);
assert.equal(input.value,'1.01','向下滚 0.1，且不能出现 1.0099999999999998');
input.value='0.95';p.jsonEditorEntryWheel(ev(100));
assert.equal(input.value,'0.85');
assert.ok(input.dispatched>=4,'改完要通知同步链写回原始 JSON');
const miss=ev(-100);miss.target={closest:()=>null};p.jsonEditorEntryWheel(miss);
assert.equal(miss.prevented,0,'非数字字段不拦截滚轮');
""")

    def test_renaming_model_clears_sha256_on_change_only(self):
        self.run_case(r"""
const hash={value:'abc'};
const card={dataset:{jsonIndex:'0'},querySelector:sel=>sel.includes('sha256')?hash:null};
const target={closest:sel=>sel==='[data-json-index]'?card:null,matches:()=>false,
  dataset:{jsonField:'name'},type:'text',value:'moss/y.safetensors'};
p.comfyJsonEditor.rows=[{name:'moss/x.safetensors',sha256:'abc'}];
p.jsonEditorEntryChanged({target,type:'input'});
assert.equal(p.comfyJsonEditor.rows[0].sha256,'abc','输入过程中不能抹哈希');
p.jsonEditorEntryChanged({target,type:'change'});
assert.equal(p.comfyJsonEditor.rows[0].sha256,'');
assert.equal(hash.value,'');
""")

    def test_image_container_exact_match_and_ordered_references(self):
        self.run_case(r"""
const slot={class_type:'ImageContainer',field_name:'selected_json'};
assert.equal(p.isImageContainerSlot(slot),true);
assert.ok(p.defaultVisibleKeys([{...slot,node_id:'42',field_type:'text'}]).has('42|selected_json'),
  '导入节点后默认显示选图入口，不要求手动启用参数');
assert.equal(p.isImageContainerSlot({...slot,field_name:'pool_json'}),false);
assert.equal(p.isImageContainerSlot({...slot,class_type:'OtherNode'}),false);
const old={name:'old.png',subfolder:'images',type:'input',custom:{keep:true}};
const rows=[old,{name:'new.png',_cat_chat_media:{path:'managed-new.png'}}];
assert.equal(JSON.stringify(p.imageContainerRows(JSON.stringify(rows))),JSON.stringify(rows));
assert.equal(p.imageContainerReferences(slot,JSON.stringify(rows))[0].path,'managed-new.png');
assert.equal(p.imageContainerReferences({...slot,field_name:'pool_json'},JSON.stringify(rows)).length,0);
assert.equal(p.imageContainerRows('').length,0);
assert.equal(p.imageContainerValueError([]),'','清空选择必须合法');
assert.equal(p.imageContainerValueError(rows),'');
assert.equal(p.imageContainerValueError([{name:'x',type:''}]),'','兼容插件的空来源默认值');
assert.notEqual(p.imageContainerValueError(Array(7).fill(old)),'');
assert.notEqual(p.imageContainerValueError([{name:''}]),'');
p.imageContainerMissingPaths.add('managed-new.png');
assert.match(p.imageContainerValueError(rows),/失效/);
assert.equal(p.imageContainerValueError([old]),'','移除失效图后可继续使用其他图片');
""")

    def test_image_container_preview_only_accepts_managed_paths(self):
        self.run_case(r"""
context.state.bootstrap={data_location:{data_dir:'/sandbox/data'}};
assert.equal(p.imageContainerManagedPath('/sandbox/data/uploads/new.png'),true);
for (const path of ['https://example.invalid/tracker.png','/sandbox/secret.png',
  '/sandbox/data/uploads/../../secret.png','/sandbox/data/uploads-other/new.png']) {
  assert.equal(p.imageContainerManagedPath(path),false,path);
}
context.api=async()=>{throw new Error('外部地址不得发送到文件接口');};
(async()=>{
 const missing=await p.checkImageContainerFiles([{path:'https://example.invalid/tracker.png'}]);
 assert.equal(missing.length,1);
 assert.ok(p.imageContainerMissingPaths.has(missing[0]));
})().catch((error)=>{console.error(error);process.exitCode=1;});
""")

    def test_dialog_wiring_present(self):
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        js = (ROOT / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        # 原生 datalist 会按输入框现值过滤（现存的整条路径和候选清单可能一个都对不上），
        # 表现为「有下拉箭头但点了没反应」，所以换成弹窗内自绘候选。
        self.assertNotIn("comfyuiJsonLoraNames", html)
        self.assertNotIn("comfyuiJsonLoraNames", js)
        self.assertIn("void prepareJsonEditorLoraCandidates();", js)
        self.assertIn("attachCandidateCombo(input, null, () => modelCandidatesFor(\"lora\")", js)
        self.assertIn("{ container: dialog, notify: true }", js)
        self.assertIn("addEventListener(\"wheel\", jsonEditorEntryWheel, { passive: false })", js)
        # 条目瘦身作用域覆盖（全局 label input 的 40px 高不在这里改）。
        rule = re.search(r"\.comfyui-json-dialog \.comfyui-json-field input\[data-json-field\]\s*\{([^}]+)\}", css)
        self.assertIsNotNone(rule)
        self.assertRegex(rule.group(1), r"height:\s*26px")
        self.assertRegex(rule.group(1), r"margin-top:\s*0")
        self.assertIn(".comfyui-json-dialog .comfyui-json-enabled input[type=\"checkbox\"]", css)
        self.assertIn(".comfyui-json-dialog .comfyui-combo-list-inset { position: absolute;", css)
        entries = re.search(r"\.comfyui-json-entries\s*\{([^}]+)\}", css)
        self.assertIsNotNone(entries)
        self.assertRegex(entries.group(1), r"max-height:\s*min\(58vh,\s*520px\)")


if __name__ == "__main__":
    unittest.main()
