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

    def test_media_numbers_match_mapping_headers_and_mobile_without_changing_file_indices(self):
        self.run_case(r"""
load('24-comfyui.js','globalThis.p={batchPortsMarkup,batchItemsMarkup};');
ctx.attachmentThumbUrl=record=>record.path;
const params=['image','audio','video','image','audio','video','image'].map((media,index)=>({
  node_id:String(index+1),field_name:media,field_type:media,media,default:'None',media_optional:false,
}));
const shortNames={image:'图',audio:'音',video:'视频'},fullNames={image:'图片',audio:'音频',video:'视频'};
const cases=[
  {name:'混排',order:[0,1,2,3],numbers:[1,1,1,2]},
  {name:'重排',order:[3,2,0,1],numbers:[1,1,2,1]},
  {name:'首口禁用',order:[0,1,2,3],disabled:[0],numbers:[0,1,1,1]},
  {name:'混排过滤',order:[0,1,2,3,4,5],disabled:[0,2],numbers:[0,1,0,1,2,1]},
  {name:'同类多口',order:[6,3,0],numbers:[1,2,3]},
  {name:'全部禁用',order:[0,1,2,3],disabled:[0,1,2,3],numbers:[0,0,0,0]},
];
for(const test of cases){
  const ports=test.order.map((index,position)=>({node_id:params[index].node_id,field_name:params[index].field_name,
    checked:!(test.disabled||[]).includes(position)}));
  const active=ports.filter(port=>port.checked);
  const files=Object.fromEntries(active.map((port,index)=>[String(index),{
    name:`槽位-${port.node_id}-文件`,path:`/managed/${port.node_id}.png`,
  }]));
  const base={name:test.name,script:'',prompt:'',duration:'',resolution:'',shots:1};
  const scheme={id:'s1',detail:{params},mapping:{media_ports:ports,extra_points:[]},fixed:{},items:[
    {...base,files:{}},
    {...base,files},
    {...base,files,skips:Object.fromEntries(active.map(port=>[`${port.node_id}|${port.field_name}`,true]))},
  ]};
  const before=JSON.stringify(scheme);
  const expectedNumbers=test.numbers.map((number,index)=>number?`${shortNames[params[test.order[index]].media]} ${number}`:'—');
  const mapping=ctx.p.batchPortsMarkup(scheme),html=ctx.p.batchItemsMarkup(scheme,0);
  assert.deepEqual([...mapping.matchAll(/<span class="cbm-no">([^<]+)<\/span>/g)].map(match=>match[1]),expectedNumbers,test.name+' 配置编号');
  assert.deepEqual([...html.matchAll(/<th>((?:图|音|视频) \d+)（/g)].map(match=>match[1]),
    expectedNumbers.filter(number=>number!=='—'),test.name+' 表头编号');
  const expectedLabels=test.numbers.flatMap((number,index)=>number?[
    `${fullNames[params[test.order[index]].media]} ${number} · ${params[test.order[index]].node_id}`]:[]);
  const rows=[...html.matchAll(/<tr>([\s\S]*?)<\/tr>/g)].map(match=>match[1]).filter(row=>row.includes('data-batch-item="name"'));
  assert.equal(rows.length,3);
  rows.forEach((row,rowIndex)=>{
    const cells=[...row.matchAll(/<td data-label="([^"]+)">([\s\S]*?)<\/td>/g)].filter(match=>match[2].includes('data-batch-tile'));
    assert.deepEqual(cells.map(cell=>cell[1]),expectedLabels,test.name+' 手机编号');
    cells.forEach((cell,portIndex)=>{
      assert.ok([...cell[2].matchAll(/data-port="(\d+)"/g)].every(match=>Number(match[1])===portIndex),
        test.name+' 事件必须保留启用口的整体索引');
      if(rowIndex>0)assert.ok(cell[2].includes(`槽位-${active[portIndex].node_id}-文件`),test.name+' 文件仍绑定原槽位');
      if(rowIndex===2)assert.ok(cell[2].includes('data-batch-skipped="1"'),'手动跳过不改变显示编号');
    });
  });
  assert.equal(JSON.stringify(scheme),before,test.name+' 纯呈现不得修改素材、顺序或映射');
}
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
