"""贴签匠逐图结果预览：批次转终态后不能把已渲染的「按图」结果清掉。

远端反馈（2026-11-10）：打标过程中逐图卡片（标签/描述/TXT 状态）看得见，
批次一结束结果区就变成「还没有逐图结果」并停在那里。根因是终态详情接口不带
items，前端先清空列表渲染空态、再由 loadTaskItems 补回来；补回来的签名与上一轮
一致，被懒加载签名 diff 挡掉，空态就一直挂着。

这里用 Node + vm 跑真实的 23-labeler.js 函数：不加载浏览器也能钉住渲染行为。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function node(tag='div'){
  const el={tagName:tag,children:[],className:'',textContent:'',title:'',hidden:false,disabled:false,
    dataset:{},style:{},classList:{add(){},remove(){},toggle(){},contains(){return false}},
    append(...kids){for(const kid of kids)el.children.push(kid);},
    replaceChildren(...kids){el.children=[...kids];},
    addEventListener(){},setAttribute(){},removeAttribute(){}};
  return el;
}
const resultBody=node('div'),hint=node('p');
const elements={'#labelerResultBody':resultBody,'#labelerResultsHint':hint};
let apiImpl=async()=>({});
const context=vm.createContext({
  console,$:key=>elements[key]||null,$$:()=>[],
  api:(url,opts)=>apiImpl(url,opts),
  state:{},onStudioPageChange(){},setTimeout,clearTimeout,setInterval,clearInterval,
  document:{addEventListener(){},createElement:tag=>node(tag)},
  window:{addEventListener(){},matchMedia:()=>({matches:false})},
});
const source=fs.readFileSync('public/js/23-labeler.js','utf8')
  .replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
vm.runInContext(source+'\nglobalThis.p={labeler,renderResultBody,pollTask};',context);
const p=context.p;
function seed(){
  p.labeler.taskId='tg-test';
  p.labeler.task={status:'running',items_total:1,progress:{done:1,total:1}};
  p.labeler.items=[{item_id:'i1',state:'succeeded',annotation_id:1,
    relative_path:'d/a0.png',name:'a0.png',txt_state:'written',txt_path:'d/a0.txt',txt_chars:16}];
  p.labeler.annotations=[{id:1,tags:['cat'],description:'一只猫坐在窗台上'}];
  p.labeler.annotationTotal=1;
  p.renderResultBody();
}
function card(){return resultBody.children[0];}
function assertCard(label){
  assert.equal(resultBody.children.length,1,label+'：应只有一张卡');
  assert.equal(card().className,'labeler-anno',label+'：应渲染逐图卡而不是空态');
  assert.equal(card().children[0].children[0].textContent,'d/a0.png',label+'：卡片标题');
}
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行贴签匠前端")
class LabelerResultsPreviewTests(unittest.TestCase):
    def run_case(self, body: str) -> None:
        result = subprocess.run([shutil.which("node"), "-"], input=HARNESS + body,
                                cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_terminal_wipe_then_restore_redraws_cards(self):
        """终态清空 items 后补拉回来，必须重绘逐图卡（签名 diff 不能挡住）。"""
        self.run_case(r"""
seed();assertCard('打标中');
p.labeler.items=[];p.renderResultBody();
assert.equal(resultBody.children[0].children[0].textContent,'还没有逐图结果');
seed();  // 补拉回来（同样的 items / annotations）
assertCard('补拉后');
""")

    def test_poll_terminal_keeps_rendered_items(self):
        """轮询拿到终态详情（items 为空）时，不清掉已渲染的逐图列表。"""
        self.run_case(r"""
seed();
apiImpl=async(url)=>{
  if(url.includes('/annotations'))return{annotations:[{id:1,tags:['cat'],description:'一只猫坐在窗台上'}],total:1};
  if(url.includes('/items'))return{items:[],total:1,count:0};
  if(url.includes('/api/labeler/tasks/tg-test'))return{task:{id:'tg-test',status:'done',
    progress:{done:1,total:1,failed:0,uncertain:0,cancelled:0},items:[],items_total:1}};
  if(url.includes('/api/labeler/tasks'))return{tasks:[]};
  throw new Error('unexpected request '+url);
};
(async()=>{await p.pollTask();
  assert.equal(p.labeler.items.length,1,'终态轮询不该清空 items');
  assertCard('转终态后');
})().catch((err)=>{console.error(err);process.exit(1);});
""")
