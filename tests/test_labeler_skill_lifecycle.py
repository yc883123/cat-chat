"""执行真实贴签匠函数：选用 Skill 不回填正文，手输规则和选择互不覆盖。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function element(value='') {return {value,disabled:false,textContent:'',dataset:{},handlers:{},classList:{toggle(){}},
  addEventListener(type,fn){this.handlers[type]=fn;}};}
const prompt=element('original'),select=element('skill-a'),note=element(),page=element(),output=element();
select.selectedOptions=[{textContent:'Skill A'}];
page.dataset.labelerPage='custom';
const elements={'#labelerCustomSystemPrompt':prompt,'#labelerSkillSelect':select,
  '#labelerSkillNote':note,'#studioPageLabeler':page,'#labelerCustomOutputRow':output};
let pageListener,calls=0;
const context=vm.createContext({console,$:key=>elements[key]||null,$$:()=>[],api:async()=>{calls++;throw new Error('unexpected request');},
  state:{},onStudioPageChange:fn=>{pageListener=fn;},setTimeout,clearTimeout,setInterval,clearInterval});
const source=fs.readFileSync('public/js/23-labeler.js','utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
vm.runInContext(source+'\nglobalThis.p={applySelectedSkill,clearSystemPrompt,setLabelerPage,bindLabelerEvents,labeler};',context);
const p=context.p;p.setLabelerPage('custom');p.bindLabelerEvents();
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行贴签匠前端")
class LabelerSkillLifecycleTests(unittest.TestCase):
    def run_case(self, body: str) -> None:
        result = subprocess.run([shutil.which("node"), "-"], input=HARNESS + body,
                                cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_selection_does_not_fetch_or_replace_manual_prompt(self):
        self.run_case(r"""
select.handlers.change();
assert.equal(p.labeler.skillPick.id,'skill-a');assert.equal(prompt.value,'original');
assert.equal(output.hidden,true);assert.equal(calls,0);assert.match(note.textContent,/对话同款链路/);
select.value='skill-b';select.selectedOptions=[{textContent:'Skill B'}];select.handlers.change();
assert.equal(p.labeler.skillPick.id,'skill-b');assert.equal(prompt.value,'original');assert.equal(calls,0);
""")

    def test_clear_resets_skill_and_prompt_without_pending_write(self):
        self.run_case(r"""
select.handlers.change();p.clearSystemPrompt();
assert.equal(prompt.value,'');assert.equal(select.value,'');assert.equal(p.labeler.skillPick,null);
assert.equal(output.hidden,false);assert.equal(calls,0);
""")

    def test_manual_input_and_page_changes_preserve_user_content(self):
        self.run_case(r"""
select.handlers.change();prompt.value='manual replacement';p.setLabelerPage('preset');
pageListener('library',{closed:true});p.setLabelerPage('custom');
assert.equal(prompt.value,'manual replacement');assert.equal(p.labeler.skillPick.id,'skill-a');
select.value='';select.handlers.change();assert.equal(prompt.value,'manual replacement');
assert.equal(p.labeler.skillPick,null);assert.equal(output.hidden,false);assert.equal(calls,0);
""")

    def test_long_manual_prompt_survives_skill_selection_and_deselection(self):
        self.run_case(r"""
prompt.value='x'.repeat(24001)+'tail rule';select.handlers.change();
select.value='';select.handlers.change();
assert.equal(prompt.value,'x'.repeat(24001)+'tail rule');assert.equal(calls,0);
""")


if __name__ == "__main__":
    unittest.main()
