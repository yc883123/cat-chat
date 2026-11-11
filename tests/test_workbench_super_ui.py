"""工作台改版：任务总管状态、增量刷新与轮询的可执行前端守门。"""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "需要 Node 执行真实前端函数")
class WorkbenchSuperUITests(unittest.TestCase):
    def test_rendering_and_background_refresh(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync('public/js/28-super-agent.js', 'utf8').replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
const elements = new Map();
const $ = key => {
  if (!elements.has(key)) {
    const node = {value:'', hidden:false, dataset:{}, attrs:{}, writes:0, html:'', content:'',
      classList:{add(){},remove(){},toggle(){}}, querySelectorAll(){return []},
      setAttribute(k,v){this.attrs[k]=v}, removeAttribute(k){delete this.attrs[k]},
      set innerHTML(v){this.writes++;this.html=v}, get innerHTML(){return this.html},
      set textContent(v){this.writes++;this.content=String(v)}, get textContent(){return this.content}};
    elements.set(key,node);
  }
  return elements.get(key);
};
const context = {$, assert, console, process, globalThis, setTimeout, clearTimeout, state:{appView:'super-agent',conversations:[]},
  document:{visibilityState:'visible',createElement:()=>({content:{children:[]},innerHTML:''})}, escapeHtml:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'), localFileUrl:v=>v};
vm.runInNewContext(source + `
(async()=>{
  const detail={thread_id:'t', title:'检查任务', status:'active', current_node_id:'n', linked_conversation_ids:[],updated_at:100};
  const node={node_id:'n',executor_type:'chat_run',kind:'chat',status:'ready',capabilities:{start:true}};
  viewState.selectedId='t';viewState.detail=detail;viewState.nodes=[node];
  renderDetail();
  assert.match($('#superAgentFocusTitle').textContent,/尚未开始/);
  assert.match($('#superAgentFocusActions').innerHTML,/data-super-action="start"/);
  const writes=$('#superAgentFocusActions').writes;
  renderDetail();
  assert.equal($('#superAgentFocusActions').writes,writes,'未变内容不得替换操作按钮');
  viewState.events=[{event_id:'e',sequence:1,event_type:'thread_updated',created_at:2}];
  renderDetail();
  assert.equal($('#superAgentFocusActions').writes,writes,'只有活动变化不得替换主操作按钮');
  detail.status='needs_recovery';node.status='interrupted';node.capabilities={retry:true};renderDetail();
  assert.match($('#superAgentFocusTitle').textContent,/尚未恢复/);
  assert.ok(!$('#superAgentFocusText').textContent.includes('不需要你操作'));
  assert.match($('#superAgentFocusActions').innerHTML,/data-super-action="retry"/);
  detail.status='active';node.status='running';node.capabilities={};renderDetail();
  assert.equal($('#superAgentFocusLabel').textContent,'正在执行');
  assert.equal($('#superAgentFocusActions').innerHTML,'');
  detail.status='cancelled';renderDetail();assert.equal($('#superAgentFocus').dataset.tone,'idle');
  detail.status='archived';renderDetail();assert.equal($('#superAgentFocusTitle').textContent,'任务已归档');
  assert.equal(approvalLabel('novel_apply'),'应用改编结果');
  assert.equal(artifactTitle({title:'writer setup_all 结果'}),'织书台 · 全部设定结果');
  assert.equal(artifactSummary({summary:'阶段状态：done；仅保留引用，不复制正文'}),'已完成 · 内容保存在原作品中');
  const settled={thread_id:'t',status:'completed',updated_at:1};
  viewState.detail=settled;viewState.threads=[settled];
  assert.equal(refreshDelay(),30000,'全部结束仅低频检查列表');
  viewState.threads.push({thread_id:'other',status:'active'});assert.equal(refreshDelay(),4000);
  const requests=[];api=async url=>{requests.push(url);return {threads:[settled,{thread_id:'other',status:'active'}]}};
  await loadThreads({background:true});
  assert.equal(requests.length,1,'浏览已结束任务时不重复读取详情五接口');
  let failOnce=true, detailCalls=0;
  viewState.detail={thread_id:'t',status:'active'};viewState.threads=[{thread_id:'t',status:'active'}];
  api=async url=>{
    if(url.includes('?include_archived'))return {threads:[settled]};
    if(url.endsWith('/t')){detailCalls++;if(failOnce){failOnce=false;throw new Error('临时503')}return {thread:settled};}
    return {nodes:[],events:[],artifacts:[],approvals:[],last_sequence:0};
  };
  await loadThreads({background:true});
  assert.match(viewState.error,/临时503/);assert.equal(viewState.detail.status,'active');
  await loadThreads({background:true});
  assert.equal(detailCalls,2,'终态详情失败后，列表快照虽未变化也要自动重试');
  assert.equal(viewState.detail.status,'completed');assert.equal(viewState.error,'');
  const waits=[];api=url=>new Promise(resolve=>waits.push({url,resolve}));
  const older=loadThreads();const newer=loadThreads();
  waits[1].resolve({threads:[]});await newer;
  waits[0].resolve({threads:[{thread_id:'stale',status:'active'}]});await older;
  assert.equal(viewState.threads.length,0,'迟到列表不得覆盖最新列表');
  assert.equal($('#superAgentTitle').textContent,'任务进展与结果');
  assert.equal($('#superAgentStatus').textContent,'');assert.equal($('#superAgentStatus').hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1});
`, context);
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                                encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
