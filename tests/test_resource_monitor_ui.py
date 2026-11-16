"""资源监控：不启动浏览器，执行真实控制器的轮询与焦点生命周期。"""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r"""
import fs from 'node:fs';
import assert from 'node:assert/strict';
const source=fs.readFileSync('public/js/22f-resource-monitor.js','utf8');
const {createResourceMonitor}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
class Element {
 constructor(){this.hidden=false;this.isConnected=true;this.dataset={};this.events={};this.attrs={};this.nodes={};this.children=[];this.textContent='';this.open=false;}
 addEventListener(n,cb){(this.events[n]??=[]).push(cb);}
 removeEventListener(n,cb){this.events[n]=(this.events[n]||[]).filter(x=>x!==cb);}
 fire(n,event={}){for(const cb of this.events[n]||[])cb(event);}
 querySelector(s){return this.nodes[s]||null;}
 closest(){return null;}
 contains(){return true;}
 append(...els){this.children.push(...els);}
 remove(){this.removed=true;}
 removeAttribute(n){delete this.attrs[n];}
 setAttribute(n,v){this.attrs[n]=v;}
 showModal(){this.open=true;}
 close(){this.open=false;this.fire('close');}
 getClientRects(){return [{}];}
 focus(){this.focused=true;}
}
let clock=100000, serial=0, observerCallback;
const timeouts=new Map(),intervals=new Map();
const timers={setTimeout:(fn,ms)=>{const id=++serial;timeouts.set(id,{fn,ms});return id;},clearTimeout:id=>timeouts.delete(id),
 setInterval:(fn,ms)=>{const id=++serial;intervals.set(id,{fn,ms});return id;},clearInterval:id=>intervals.delete(id)};
const doc=new Element(),ids={};doc.hidden=false;doc.getElementById=id=>ids[id]??=new Element();doc.createElement=()=>new Element();
const root=new Element(),dialog=new Element();root.hidden=true;
const monitor=doc.getElementById('workbenchResourceMonitor');
for(const kind of ['memory','gpu']){
 const summary=new Element();summary.nodes['[data-resource-full]']=new Element();summary.nodes['[data-resource-compact]']=new Element();
 monitor.nodes[`[data-resource-summary="${kind}"]`]=summary;
 monitor.nodes[`[data-resource-bar="${kind}"]`]=new Element();
}
const requests=[];
const request=(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));
class Observer {constructor(cb){observerCallback=cb;}observe(){}disconnect(){}}
const ui=createResourceMonitor({root,dialog,request,now:()=>clock,timers,document:doc,Observer});
const flush=async()=>{for(let i=0;i<5;i++)await Promise.resolve();};
const show=()=>{root.hidden=false;observerCallback();};
const hide=()=>{root.hidden=true;observerCallback();};
const data=(overrides={})=>({source:'host',sampled_at:1,memory:{available:true,total_bytes:8*1073741824,used_bytes:2*1073741824,percent:25},
 gpu_available:true,gpus:[{index:0,name:'GPU A',total_bytes:4*1073741824,used_bytes:2*1073741824,percent:50}],...overrides});
const summary=(kind,compact=true)=>monitor.querySelector(`[data-resource-summary="${kind}"]`).querySelector(compact?'[data-resource-compact]':'[data-resource-full]').textContent;
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js")
class ResourceMonitorBehaviorTests(unittest.TestCase):
    def run_js(self, body):
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-"],
                                input=HARNESS + body, cwd=ROOT, text=True,
                                encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_visible_only_single_flight_and_invalidated_reply(self):
        self.run_js(r"""
assert.equal(requests.length,0);show();assert.equal(requests.length,1);
assert.equal(requests[0].path,'/api/system/resources');assert.equal(intervals.values().next().value.ms,3000);
ui.refresh();ui.refresh();assert.equal(requests.length,1);
hide();assert.equal(intervals.size,0);assert.equal(requests[0].options.signal.aborted,true);
show();assert.equal(requests.length,1,'隐藏后的在途请求释放前不得并发');
requests[0].resolve(data());await flush();assert.equal(requests.length,2);assert.equal(summary('memory'),'不可用');
requests[1].resolve(data());await flush();assert.equal(summary('memory'),'25%');
doc.hidden=true;doc.fire('visibilitychange');assert.equal(intervals.size,0);assert.equal(summary('memory'),'不可用');
ui.dispose();
""")

    def test_clock_skew_zero_usage_multi_gpu_and_local_updates(self):
        self.run_js(r"""
show();requests[0].resolve(data({memory:{available:true,total_bytes:8*1073741824,used_bytes:0,percent:0},gpus:[
 {index:0,name:'A',total_bytes:4*1073741824,used_bytes:1073741824,percent:25},
 {index:1,name:'B',total_bytes:8*1073741824,used_bytes:6*1073741824,percent:75}]}));await flush();
assert.equal(summary('memory'),'0%');assert.equal(summary('memory',false),'0.0/8.0 GiB');assert.equal(summary('gpu'),'75%');
const list=doc.getElementById('resourceGpuList'),first=list.children[0];assert.equal(list.children.length,2);
assert.equal(first.children[2].value,25);assert.equal(first.children[2].max,100);
await ui.openDetails(new Element());assert.equal(requests.length,2);requests[1].resolve(data());await flush();
assert.equal(list.children[0],first,'刷新复用显卡行');assert.equal(first.children[1].textContent,'2.0 / 4.0 GiB · 50%');
assert.equal(list.children[1].removed,true);assert.equal(summary('memory'),'25%');ui.dispose();
""")

    def test_failure_expiry_and_unknown_usage_never_display_fake_zero(self):
        self.run_js(r"""
show();requests[0].resolve(data());await flush();assert.equal(summary('memory'),'25%');
const expiry=[...timeouts.values()].find(t=>t.ms===10001);clock+=10001;expiry.fn();
assert.equal(summary('memory'),'不可用');assert.match(doc.getElementById('resourceMonitorStatus').textContent,/过期/);
ui.refresh();requests[1].reject(new Error('secret backend code'));await flush();assert.match(doc.getElementById('resourceMonitorStatus').textContent,/读取失败/);
ui.refresh();requests[2].resolve(data({memory:{available:false,total_bytes:null,used_bytes:null,percent:null,reason:'internal_code'},gpu_available:false,gpus:[],reason:'missing_driver'}));await flush();
assert.equal(summary('memory'),'不可用');assert.equal(summary('gpu'),'不可用');
assert.equal(doc.getElementById('resourceMemoryBar').hidden,true);
assert.doesNotMatch(doc.getElementById('resourceGpuReason').textContent,/missing_driver/);
assert.doesNotMatch(doc.getElementById('resourceMemoryReason').textContent,/internal_code/);ui.dispose();
""")

    def test_timeout_discards_late_success_and_recovers(self):
        self.run_js(r"""
show();const timeout=[...timeouts.values()].find(t=>t.ms===8000);timeout.fn();
assert.match(doc.getElementById('resourceMonitorStatus').textContent,/超时/);
requests[0].resolve(data());await flush();assert.equal(summary('memory'),'不可用');
ui.refresh();requests[1].resolve(data());await flush();assert.equal(summary('memory'),'25%');ui.dispose();
""")

    def test_dialog_refresh_escape_and_focus_returns_to_menu_trigger(self):
        self.run_js(r"""
show();requests[0].resolve(data());await flush();
const button=new Element(),trigger=doc.getElementById('novelMoreBtn'),menu=new Element();button.dataset.resourceOpener='novelMoreBtn';
button.closest=selector=>selector==='[popover]'?menu:null;menu.hidePopover=()=>menu.dismissed=true;
ui.openDetails(button);assert.equal(dialog.open,true);assert.equal(requests.length,2);assert.equal(menu.hidden,true);
let stopped=false;dialog.fire('keydown',{key:'Escape',stopPropagation:()=>stopped=true});assert.equal(stopped,true);
dialog.close();assert.equal(trigger.focused,true);assert.equal(button.focused,undefined);
ui.openDetails(monitor);dialog.close();assert.equal(monitor.focused,true);ui.dispose();
""")


class ResourceMonitorMarkupTests(unittest.TestCase):
    def test_shared_node_five_menu_entries_and_init(self):
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('id="workbenchResourceMonitor"'), 1)
        self.assertEqual(html.count('id="resourceMonitorDialog"'), 1)
        self.assertEqual(html.count(" data-resource-details "), 5)
        self.assertEqual(html.count("wb-resource-header"), 5)
        studio = (ROOT / "public/js/22-studio.js").read_text(encoding="utf-8")
        self.assertIn("export function initStudio()", studio)
        self.assertIn('bindWorkbenchDetails($("#studioView > .studio-topbar"))', studio)
        self.assertIn("createResourceMonitor", studio)


if __name__ == "__main__":
    unittest.main()
