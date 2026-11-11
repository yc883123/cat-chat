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
const clock = {now:100000}, timers = new Map(); let timerId=0;
class TestDate extends Date {static now(){return clock.now}}
const setTimer=(callback,delay)=>{const id=++timerId;timers.set(id,{callback,at:clock.now+delay});return id};
clock.tick=async elapsed=>{
  const target=clock.now+elapsed;
  while(true){
    const due=[...timers].filter(([,timer])=>timer.at<=target).sort((a,b)=>a[1].at-b[1].at)[0];
    if(!due)break;
    clock.now=due[1].at;timers.delete(due[0]);await due[1].callback();
  }
  clock.now=target;
};
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
const context = {$, assert, console, process, globalThis, clock, Date:TestDate, setTimeout:setTimer, clearTimeout:id=>timers.delete(id), state:{appView:'super-agent',conversations:[]},
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
  detail.status='failed';node.status='failed';node.capabilities={retry:true};renderDetail();
  assert.match($('#superAgentFocusActions').innerHTML,/data-super-action="retry"/,'普通聊天失败仍可重试');
  node.executor_type='job';node.kind='job';node.checkpoint={status:'failed'};
  node.capabilities={retry:true,start:true,resume:true};
  for(const status of ['active','waiting_user','paused','needs_recovery','stopping','completed','failed','cancelled','archived']){
    detail.status=status;renderDetail();
    assert.equal($('#superAgentFocusActions').innerHTML,'','Job镜像所有状态均不得呈现执行操作：'+status);
    assert.equal($('#superAgentFocusTitle').textContent,statusLabel(status));
    assert.match($('#superAgentFocusText').textContent,/回到原任务处理/);
  }
  assert.equal(approvalLabel('novel_apply'),'应用改编结果');
  assert.equal(artifactTitle({title:'writer setup_all 结果'}),'织书台 · 全部设定结果');
  assert.equal(artifactSummary({summary:'阶段状态：done；仅保留引用，不复制正文'}),'已完成 · 内容保存在原作品中');
  const settled={thread_id:'t',status:'completed',updated_at:1};
  let lateArtifacts=[];const requests=[];
  api=async url=>{
    requests.push(url);
    if(url.includes('?include_archived'))return {threads:[settled,{thread_id:'other',status:'active'}]};
    if(url.endsWith('/t'))return {thread:settled};
    return {nodes:[],events:[],artifacts:lateArtifacts,approvals:[],last_sequence:0};
  };
  await loadDetail('t');viewState.threads=[settled];
  assert.equal(refreshDelay(),30000,'全部结束仅低频检查列表');
  viewState.threads.push({thread_id:'other',status:'active'});assert.equal(refreshDelay(),4000);
  assert.match($('#superAgentArtifactList').innerHTML,/暂无产物/);
  lateArtifacts=[{artifact_id:'late',title:'终态后才保存的产物',path_or_ref:'result://late'}];
  requests.length=0;scheduleRefresh();
  await clock.tick(28000);
  assert.equal(requests.length,7,'其他任务活跃时仍每4秒校准列表');
  assert.ok(requests.every(url=>url.includes('?include_archived')),'30秒间隔内不重复读取终态详情');
  await clock.tick(4000);
  assert.equal(requests.length,13,'超过复核间隔的下次自动轮询补读五个详情接口');
  assert.match($('#superAgentArtifactList').innerHTML,/终态后才保存的产物/,'列表完全不变也必须显示晚到产物');
  requests.length=0;await clock.tick(4000);
  assert.equal(requests.length,1,'复核成功后恢复只读列表，不能每4秒拉六个请求');
  clearTimeout(refreshTimer);refreshTimer=null;
  let failOnce=true, detailCalls=0;
  const lastSuccess=viewState.syncedAt;clock.now+=30000;
  api=async url=>{
    if(url.includes('?include_archived'))return {threads:[settled,{thread_id:'other',status:'active'}]};
    if(url.endsWith('/t')){detailCalls++;if(failOnce){failOnce=false;throw new Error('临时503')}return {thread:settled};}
    return {nodes:[],events:[],artifacts:[],approvals:[],last_sequence:0};
  };
  await loadThreads({background:true});
  assert.match(viewState.error,/临时503/);assert.equal(viewState.detail.status,'completed');
  assert.equal(viewState.syncedAt,lastSuccess,'详情失败不得推迟下次复核');
  await loadThreads({background:true});
  assert.equal(detailCalls,2,'终态详情失败后，列表快照虽未变化也要自动重试');
  assert.equal(viewState.detail.status,'completed');assert.equal(viewState.error,'');
  const another={thread_id:'next',status:'completed',updated_at:1};
  const waitsDetail=[];api=url=>new Promise(resolve=>waitsDetail.push({url,resolve}));
  const staleDetail=loadDetail('t');
  viewState.selectedId='next';viewState.detail=another;viewState.threads=[another];
  requests.length=0;api=async url=>{
    requests.push(url);
    return url.includes('?include_archived')?{threads:[another]}:url.endsWith('/next')?{thread:another}:{nodes:[],events:[],artifacts:[],approvals:[]};
  };
  await loadThreads({background:true});
  assert.equal(requests.length,6,'另一线程的成功时间不能抑制新线程的首次详情');
  assert.equal(viewState.syncedThreadId,'next');
  const nextSuccess=viewState.syncedAt;clock.now+=1000;
  waitsDetail.forEach(({url,resolve})=>resolve(url.endsWith('/t')?{thread:settled}:{nodes:[],events:[],artifacts:lateArtifacts,approvals:[]}));
  await staleDetail;
  assert.equal(viewState.detail.thread_id,'next');assert.equal(viewState.syncedThreadId,'next');
  assert.equal(viewState.syncedAt,nextSuccess,'旧线程迟到回包不得改写新线程的成功时间');
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
