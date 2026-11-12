"""执行真实贴签匠函数：选用 Skill 不回填正文，手输规则和选择互不覆盖。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function element(value='') {return {value,checked:false,disabled:false,textContent:'',dataset:{},handlers:{},classList:{toggle(){}},
  addEventListener(type,fn){this.handlers[type]=fn;}};}
const prompt=element('original'),select=element('skill-a'),note=element(),page=element(),output=element();
const writePreset=element(),writeCustom=element();
select.selectedOptions=[{textContent:'Skill A'}];
page.dataset.labelerPage='custom';
const elements={'#labelerCustomSystemPrompt':prompt,'#labelerSkillSelect':select,
  '#labelerSkillNote':note,'#studioPageLabeler':page,'#labelerCustomOutputRow':output,
  '#labelerWriteTxtPreset':writePreset,'#labelerWriteTxtCustom':writeCustom};
let pageListener,calls=0;
const context=vm.createContext({console,$:key=>elements[key]||null,$$:()=>[],api:async()=>{calls++;throw new Error('unexpected request');},
  state:{},onStudioPageChange:fn=>{pageListener=fn;},setTimeout,clearTimeout,setInterval,clearInterval,
  document:{addEventListener(){}},window:{addEventListener(){}}});
const shared=fs.readFileSync('public/js/22a-workbench-ui.js','utf8').replace(/^export /gm,'');
vm.runInContext(shared,context);
const source=fs.readFileSync('public/js/23-labeler.js','utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
vm.runInContext(source+'\nglobalThis.p={applySelectedSkill,clearSystemPrompt,setLabelerPage,bindLabelerEvents,collectConfig,labeler};',context);
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
assert.equal(output.hidden,true);assert.equal(calls,0);assert.match(note.textContent,/共同生效/);
select.value='skill-b';select.selectedOptions=[{textContent:'Skill B'}];select.handlers.change();
assert.equal(p.labeler.skillPick.id,'skill-b');assert.equal(prompt.value,'original');assert.equal(calls,0);
""")

    def test_clear_only_resets_prompt_and_keeps_skill(self):
        self.run_case(r"""
select.handlers.change();p.clearSystemPrompt();
assert.equal(prompt.value,'');assert.equal(select.value,'skill-a');assert.equal(p.labeler.skillPick.id,'skill-a');
assert.equal(output.hidden,true);assert.equal(calls,0);
""")

    def test_config_submits_manual_rules_and_skill_together(self):
        self.run_case(r"""
select.handlers.change();
const config=p.collectConfig();
assert.equal(config.system_prompt,'original');assert.equal(config.skill_id,'skill-a');assert.equal(config.engine,'chat');
select.value='';select.handlers.change();
assert.equal(p.collectConfig().system_prompt,'original');assert.equal(p.collectConfig().skill_id,undefined);
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

    def test_write_txt_switches_are_independent_per_page(self):
        # 两个页签各记各的开关：提交 config 只取当前页签的值，互不覆盖。
        self.run_case(r"""
p.setLabelerPage('preset');
writePreset.checked=true;writePreset.handlers.change();
assert.equal(p.collectConfig().write_txt,true);
p.setLabelerPage('custom');
assert.equal(p.collectConfig().write_txt,false);
writeCustom.checked=true;writeCustom.handlers.change();
assert.equal(p.collectConfig().write_txt,true);
assert.equal(p.labeler.writeTxtPreset,true);
assert.equal(p.labeler.writeTxtCustom,true);
writeCustom.checked=false;writeCustom.handlers.change();
assert.equal(p.collectConfig().write_txt,false);
assert.equal(p.labeler.writeTxtPreset,true);
assert.equal(calls,0);
""")


if __name__ == "__main__":
    unittest.main()
