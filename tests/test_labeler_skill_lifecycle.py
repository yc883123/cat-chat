"""执行真实贴签匠函数：Skill 迟到回包不能覆盖新输入，长文本不能静默截断。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const defer=()=>{let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j;});return {promise,resolve,reject};};
function element(value='') {return {value,disabled:false,textContent:'',dataset:{},handlers:{},classList:{toggle(){}},
  addEventListener(type,fn){this.handlers[type]=fn;}};}
const prompt=element('original'),select=element('skill-a'),full=element(),body=element(),note=element(),page=element();
page.dataset.labelerPage='custom';
const elements={'#labelerCustomSystemPrompt':prompt,'#labelerSkillSelect':select,
  '#labelerSkillLoadFull':full,'#labelerSkillLoadBody':body,'#labelerSkillNote':note,'#studioPageLabeler':page};
let pageListener;
const context=vm.createContext({console,$:key=>elements[key]||null,$$:()=>[],api:async()=>({}),
  state:{},onStudioPageChange:fn=>{pageListener=fn;},setTimeout,clearTimeout,setInterval,clearInterval});
const source=fs.readFileSync('public/js/23-labeler.js','utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
vm.runInContext(source+'\nglobalThis.p={loadSkillContent,clearSystemPrompt,setLabelerPage,bindLabelerEvents};',context);
const p=context.p;p.setLabelerPage('custom');p.bindLabelerEvents();
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行贴签匠前端")
class LabelerSkillLifecycleTests(unittest.TestCase):
    def run_case(self, body: str) -> None:
        result = subprocess.run([shutil.which("node"), "-"], input=HARNESS + body,
                                cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_clear_edit_and_selection_change_invalidate_late_success(self):
        self.run_case(r"""
(async()=>{
for(const change of [()=>p.clearSystemPrompt(),()=>{prompt.value='manual';prompt.handlers.input();},
  ()=>{select.value='skill-b';select.handlers.change();},()=>p.setLabelerPage('preset'),
  ()=>pageListener('library',{closed:true})]){
  p.setLabelerPage('custom');select.value='skill-a';prompt.value='original';
  const later=defer();context.api=()=>later.promise;const work=p.loadSkillContent('full');
  assert.equal(full.disabled,true);assert.equal(body.disabled,true);
  change();const expected=prompt.value;later.resolve({name:'old',content:'old response'});await work;
  assert.equal(prompt.value,expected);assert.equal(full.disabled,false);assert.equal(body.disabled,false);
}
})();
""")

    def test_latest_load_mode_wins_and_old_failure_cannot_replace_feedback(self):
        self.run_case(r"""
(async()=>{
const old=defer(),latest=defer();let calls=0;context.api=()=>++calls===1?old.promise:latest.promise;
const first=p.loadSkillContent('full'),second=p.loadSkillContent('body');
old.reject(new Error('old failure'));await first;
assert.equal(full.disabled,true);assert.equal(body.disabled,true);assert.equal(note.textContent,'正在读取 Skill…');
latest.resolve({name:'new',content:'whole',body:'body only'});await second;
assert.equal(prompt.value,'body only');assert.match(note.textContent,/正文/);assert.equal(full.disabled,false);
})();
""")

    def test_programmatic_edit_is_protected_even_without_input_event(self):
        self.run_case(r"""
(async()=>{const later=defer();context.api=()=>later.promise;const work=p.loadSkillContent('full');
prompt.value='programmatic replacement';later.resolve({content:'old response'});await work;
assert.equal(prompt.value,'programmatic replacement');assert.equal(full.disabled,false);})();
""")

    def test_overlong_skill_preserves_prompt_and_reports_failure(self):
        self.run_case(r"""
(async()=>{context.api=async()=>({name:'long',content:'x'.repeat(12001)});await p.loadSkillContent('full');
assert.equal(prompt.value,'original');assert.match(note.textContent,/超过提示词上限 12000/);
context.api=async()=>({name:'fits',content:'x'.repeat(12000)});await p.loadSkillContent('full');
assert.equal(prompt.value.length,12000);assert.match(note.textContent,/完整 Skill/);})();
""")


if __name__ == "__main__":
    unittest.main()
