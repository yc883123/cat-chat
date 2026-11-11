"""媒体工作台改版：保留控件归属、完整接线信息和移动端操作。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function node(tag='div') {
  return {tagName:tag,children:[],dataset:{},className:'',textContent:'',title:'',open:false,
    value:'',style:{},classList:{add(){},remove(){},toggle(){}},
    append(...children){this.children.push(...children)},setAttribute(){},addEventListener(){},
    querySelector(){return null},querySelectorAll(){return []}};
}
const elements={};
const ctx=vm.createContext({console,document:{createElement:node,querySelectorAll:()=>[]},
  $:s=>elements[s]||null,$$:()=>[],window:{},state:{},
  localStorage:{getItem(){return null},setItem(){}},setTimeout,clearTimeout,setInterval,clearInterval});
function load(file,expose){
  const src=fs.readFileSync('public/js/'+file,'utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
  vm.runInContext(src+'\n'+expose,ctx);
}
"""


class _Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.parents = {}
        self.attrs = {}

    def handle_starttag(self, tag, attrs):
        item = dict(attrs)
        if item.get("id"):
            self.parents[item["id"]] = [entry[1].get("id") for entry in self.stack]
            self.attrs[item["id"]] = item
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, item))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class MediaWorkbenchMarkupTests(unittest.TestCase):
    def test_headers_keep_original_navigation_and_control_ownership(self):
        parsed = _Markup()
        parsed.feed((ROOT / "public/index.html").read_text(encoding="utf-8"))
        self.assertIn("data-wb-page-header", parsed.attrs["labelerPanes"])
        for identifier in ("labelerStart", "labelerContinue", "labelerCancel", "labelerMaxTags"):
            self.assertIn("studioPageLabeler", parsed.parents[identifier])
        for identifier in ("comfyuiPanes", "comfyuiStatusbar", "comfyuiSaveCurrentAs", "comfyuiFreeModels"):
            self.assertIn("studioPageComfyui", parsed.parents[identifier])
        self.assertIn("comfyuiStatusbar", parsed.parents["comfyuiEditUrl"])
        self.assertNotIn("comfyuiColumns", parsed.parents["comfyuiStatusbar"])
        self.assertIn("comfyuiRunSection", parsed.parents["comfyuiSubmit"])

    def test_responsive_uses_container_state_not_only_viewport(self):
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        for size in ("medium", "narrow"):
            self.assertIn(f'#studioPageLabeler[data-wb-size="{size}"]', css)
        self.assertIn('.labeler-config-body { flex: 1;', css)
        self.assertIn('.labeler-config-foot { flex: none;', css)
        self.assertIn('td[data-label]::before', css)
        self.assertIn('.cbm-mobile-tools { display: flex;', css)


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行真实前端函数")
class MediaWorkbenchRuntimeTests(unittest.TestCase):
    def run_case(self, body):
        result = subprocess.run([shutil.which("node"), "-"], input=HARNESS + body,
                                cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_connections_start_closed_and_keep_full_mapping_as_plain_text(self):
        self.run_case(r"""
load('24-comfyui.js','globalThis.p={comfyui,renderParamConnections,slotDisplayLabel,renderSlot};');
const slots=[
  {node_id:'6',field_name:'text',role:'positive_prompt',field_type:'text',label:'6 · 提示词 · text → positive',default:'原始提示词'},
  {node_id:'10',field_name:'lora_name',role:'lora',field_type:'text',label:'10 · LoRA · lora_name\n输出 0 → model\n输出 1 → clip <img onerror=x>'},
  {node_id:'11',field_name:'lora_name',role:'lora',field_type:'text',label:'11 · LoRA · lora_name → model'},
];
ctx.p.comfyui.detail={id:'workflow',params:slots};
const details=ctx.p.renderParamConnections(slots);
assert.equal(details.tagName,'details');assert.equal(details.open,false);
assert.equal(details.children[0].textContent,'每个参数接到工作流的哪里');
const list=details.children[1];assert.equal(list.children.length,6);
assert.equal(list.children[3].textContent,slots[1].label);
assert.equal(list.children[3].children.length,0,'映射只能作为文本，不能执行标记');
assert.equal(list.children[2].textContent,'LoRA 模型（节点 10）');
assert.equal(list.children[4].textContent,'LoRA 模型（节点 11）');
const input=ctx.p.renderSlot(slots[0]);
assert.equal(input.children[0].textContent,'正向提示词');
assert.equal(input.children[0].title,slots[0].label);
assert.equal(input.children[1].value,'原始提示词');
assert.equal(input.children[1].dataset.comfyuiSlot,'6|text');
""")

    def test_resolution_names_and_unknown_slot_are_not_ambiguous(self):
        self.run_case(r"""
load('24-comfyui.js','globalThis.p={slotDisplayLabel};');
assert.equal(ctx.p.slotDisplayLabel({role:'resolution',field_name:'height'}),'分辨率（高）');
assert.equal(ctx.p.slotDisplayLabel({role:'resolution',field_name:'width'}),'分辨率（宽）');
assert.equal(ctx.p.slotDisplayLabel({node_title:'自定义节点',field_name:'text'}),'自定义节点 · text');
""")

    def test_mobile_batch_cells_keep_values_labels_and_original_actions(self):
        self.run_case(r"""
load('24-comfyui.js','globalThis.p={batchItemsMarkup};');
const scheme={id:'s1',detail:{params:[]},mapping:{media_ports:[],extra_points:[]},fixed:{},
  items:[{name:'条目 <1>',script:'完整剧本',prompt:'<img onerror=x>',duration:'6',resolution:'1024',shots:2,files:{}}]};
const html=ctx.p.batchItemsMarkup(scheme,0);
for(const label of ['名称','剧本','提示词','时长','分辨率','条数','操作'])assert.ok(html.includes('data-label="'+label+'"'));
for(const action of ['convert-item','copy-item','remove-item','add-item','clear-prompts'])assert.ok(html.includes('data-batch-action="'+action+'"'));
assert.ok(html.includes('data-batch-item="prompt"'));assert.ok(html.includes('&lt;img onerror=x&gt;'));
assert.ok(html.includes('完整剧本'));assert.ok(html.includes('cbm-mobile-tools'));
assert.equal(scheme.items[0].prompt,'<img onerror=x>','纯呈现不得修改原始字段');
""")

    def test_labeler_view_switch_preserves_draft_and_result_rows(self):
        self.run_case(r"""
load('23-labeler.js','globalThis.p={labeler,setLabelerPane};');
elements['#studioPageLabeler']=node();
ctx.p.labeler.items=[{item_id:'image-1'}];
for(const pane of ['results','queue','config']) {
  ctx.p.setLabelerPane(pane);
  assert.equal(elements['#studioPageLabeler'].dataset.labelerPane,pane);
  assert.equal(ctx.p.labeler.items[0].item_id,'image-1');
}
ctx.p.setLabelerPane('unknown');assert.equal(ctx.p.labeler.pane,'config');
""")
