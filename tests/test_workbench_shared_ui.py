"""工作台展示底座：真实 JavaScript 执行及作用域契约。"""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "需要 Node.js")
class WorkbenchSharedBehaviorTests(unittest.TestCase):
    def run_js(self, body):
        script = """
import fs from 'node:fs';
import assert from 'node:assert/strict';
const source = fs.readFileSync('public/js/22a-workbench-ui.js', 'utf8');
const ui = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
""" + body
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-"],
                                input=script, text=True, encoding="utf-8", capture_output=True,
                                cwd=ROOT, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_container_width_not_window_and_hidden_does_not_reset(self):
        self.run_js("""
let width=1200, resize, disconnects=0, removed=0;
globalThis.window={innerWidth:1440, addEventListener(){}, removeEventListener(){removed++;}};
globalThis.ResizeObserver=class { constructor(cb){resize=cb;} observe(){} disconnect(){disconnects++;} };
const root={dataset:{},getBoundingClientRect:()=>({width})};
const changes=[];const dispose=ui.watchWorkbenchSize(root, state=>changes.push(state));
assert.equal(root.dataset.wbSize,'wide');
width=850;resize();assert.equal(root.dataset.wbSize,'medium');
width=760;resize();assert.equal(root.dataset.wbSize,'narrow');
width=0;resize();assert.equal(changes.length,3);assert.equal(root.dataset.wbSize,'narrow');
width=1200;resize();assert.equal(root.dataset.wbSize,'wide');
dispose();assert.equal(disconnects,1);assert.equal(removed,1);
""")

    def test_no_resize_observer_and_missing_element_are_safe(self):
        self.run_js("""
let resize;globalThis.window={addEventListener:(name,cb)=>{resize=cb;},removeEventListener(){}};
assert.equal(typeof ui.watchWorkbenchSize(null),'function');
const root={dataset:{},clientWidth:600};ui.watchWorkbenchSize(root);
assert.equal(root.dataset.wbSize,'narrow');root.clientWidth=1100;resize();assert.equal(root.dataset.wbSize,'wide');
""")

    def test_five_status_tones_do_not_mark_ready_as_running(self):
        self.run_js("""
for(const status of ['running','active','queued'])assert.equal(ui.statusTone(status),'run');
for(const status of ['waiting_user','needs_recovery','human_check'])assert.equal(ui.statusTone(status),'wait');
for(const status of ['completed','done','succeeded'])assert.equal(ui.statusTone(status),'ok');
assert.equal(ui.statusTone('failed'),'bad');
for(const status of ['ready','paused','cancelled','unknown'])assert.equal(ui.statusTone(status),'idle');
assert.equal(ui.statusLabel('ready'),'待开始');assert.equal(ui.statusLabel('needs_recovery'),'需要处理');
""")

    def test_unchanged_html_does_not_replace_dom(self):
        self.run_js("""
let html='<button>继续</button>',writes=0;
const el={get innerHTML(){return html;},set innerHTML(next){writes++;html=next;}};
assert.equal(ui.patchWorkbenchHtml(el,html),false);assert.equal(writes,0);
assert.equal(ui.patchWorkbenchHtml(el,'<button>完成</button>'),true);assert.equal(writes,1);
""")

    def test_menu_position_clamps_to_mobile_visual_viewport(self):
        self.run_js("""
globalThis.window={visualViewport:{offsetLeft:0,offsetTop:0,width:320,height:300}};
const trigger={getBoundingClientRect:()=>({left:290,bottom:280})};
const panel={style:{},getBoundingClientRect:()=>({width:240,height:200})};
ui.positionWorkbenchMenu(trigger,panel);
assert.equal(panel.style.left,'68px');assert.equal(panel.style.top,'88px');
assert.equal(panel.style.maxWidth,'296px');assert.equal(panel.style.maxHeight,'276px');
""")

    def test_menu_escape_does_not_reach_application_sidebar_handler(self):
        self.run_js("""
const targetHandlers={},panelHandlers={};let restored=0,stopped=0,prevented=0;
const trigger={isConnected:true,setAttribute(){},addEventListener:(name,cb)=>targetHandlers[name]=cb,
  getBoundingClientRect:()=>({left:10,bottom:20}),focus(){restored++;},contains:()=>false};
const panel={id:'menu',style:{},hidden:true,setAttribute(){},addEventListener:(name,cb)=>panelHandlers[name]=cb,
  querySelector:()=>null,getBoundingClientRect:()=>({width:180,height:100}),contains:()=>false,
  showPopover(){},hidePopover(){}};
globalThis.document={activeElement:trigger,addEventListener(){},documentElement:{clientWidth:390,clientHeight:800}};
globalThis.window={addEventListener(){}};
const binding=ui.bindWorkbenchMenu(trigger,panel);
assert.equal(ui.bindWorkbenchMenu(trigger,panel),binding);
targetHandlers.click();assert.equal(panel.hidden,false);
panelHandlers.keydown({key:'Escape',preventDefault(){prevented++;},stopPropagation(){stopped++;}});
assert.equal(panel.hidden,true);assert.equal(stopped,1);assert.equal(prevented,1);assert.equal(restored,1);
""")


class WorkbenchScopeTests(unittest.TestCase):
    def test_hidden_fix_is_local_and_preserves_labeler_pages(self):
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        foundation = css.split("/* wb-foundation:start */")[1].split("/* wb-foundation:end */")[0]
        self.assertIn(".writer-stage-group[hidden]", foundation)
        self.assertIn(".writer-field[hidden]", foundation)
        self.assertNotIn(".labeler-page-body[hidden]", foundation)
        self.assertNotIn("\n[hidden]", foundation)

    def test_shared_header_moves_existing_nodes_not_duplicates(self):
        source = (ROOT / "public/js/22a-workbench-ui.js").read_text(encoding="utf-8")
        self.assertIn("target.prepend(mobile)", source)
        self.assertIn("target.prepend(expand)", source)
        self.assertNotIn("cloneNode", source)


if __name__ == "__main__":
    unittest.main()
