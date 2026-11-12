"""Run real Studio JavaScript with deferred API/XHR responses, without a browser."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const studioSource=fs.readFileSync('public/js/22-studio.js','utf8').replace(/^import .*;\r?$/gm,'').replace(/\bexport /g,'');
const sharedSource=fs.readFileSync('public/js/22c-content-skills.js','utf8').replace(/^import .*;\r?$/gm,'').replace(/\bexport /g,'');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function element(tag='div') {
  let value='';const children=[],handlers={};
  const el={tagName:tag,children,handlers,dataset:{},style:{},hidden:false,disabled:false,textContent:'',open:false,
    classList:{toggle(){},add(){},remove(){}},setAttribute(){},removeAttribute(){},focus(){},remove(){},scrollIntoView(){},
    querySelector(){return null;},querySelectorAll(){return [];},closest(){return null;},
    append(...items){children.push(...items);if(tag==='select'&&!value&&children.length)value=children[0].value;},
    replaceChildren(...items){children.length=0;value='';this.append(...items);},
    addEventListener(name,fn){(handlers[name] ||= []).push(fn);},
    dispatchEvent(event){for(const fn of handlers[event.type]||[])fn(event);},
    click(){this.dispatchEvent({type:'click'});}, showModal(){this.open=true;},close(){this.open=false;},
  };
  Object.defineProperties(el,{value:{get(){return value;},set(v){value=String(v);}},options:{get(){return children;}},childElementCount:{get(){return children.length;}}});
  return el;
}
function storage() {const data=new Map();return {getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,String(v)),removeItem:k=>data.delete(k),data};}
function fixture(store=storage()) {
  const nodes=new Map(),requests=[],toasts=[],renders=[],panes=[],xhr=[];let seq=0;
  const $=selector=>{if(!nodes.has(selector))nodes.set(selector,element(selector.includes('Select')?'select':'div'));return nodes.get(selector);};
  class XHR {constructor(){this.upload={};xhr.push(this);}open(){}setRequestHeader(){}send(){}done(path){this.status=200;this.responseText=JSON.stringify({path,name:'image.png'});this.onload();}}
  const context=vm.createContext({console,Map,Set,Promise,JSON,URLSearchParams,sessionStorage:store,
    crypto:{randomUUID:()=>`key-${++seq}`},randomUuid:()=>`key-${++seq}`,$: $,api:(url,options)=>{const response=deferred();requests.push({url,options,...response});return response.promise;},
    state:{bootstrap:{settings:{studio:{}},model_profiles:[{model_key:'p1'},{model_key:'p2'}],default_model_key:'p1'}},
    toast:message=>toasts.push(message),localFileUrl:path=>path,applyModelSkinFor:()=>{},
    document:{createElement:element,querySelector:()=>null,querySelectorAll:()=>[],body:element(),dispatchEvent(){}},
    window:{setTimeout(){},addEventListener(){},confirm:()=>true},setTimeout:()=>1,setInterval:()=>1,clearInterval(){},
    XMLHttpRequest:XHR,FormData:class{append(){}},Event:class{constructor(type){this.type=type;}},
    workbenchStatusLabel:value=>value, syncWorkbenchHeader(){},filePanelState:{open:false},setAppView(){},renderSidebar(){},
    sidebarDesktop:()=>true,closeSidebar(){},openSidebar(){},applyFilePanelOpenClass(){},
  });
  vm.runInContext(sharedSource+'\n'+studioSource+`
    const actualSyncStudioModel=syncStudioModel;
    const actualReconcile=reconcileActiveTask;
    const actualRefreshTasks=refreshTasks;
    const actualImageRender=renderImageQueue;
    const actualSetResultMode=setResultMode;
    renderStudioDetail=()=>{};applyPromptSourceUi=()=>{};renderImagePreview=()=>{};
    renderStudioCategories=()=>{};renderStudioList=async()=>{};saveStudioSelection=()=>{};
    renderSkillHistory=()=>{};renderRecentTasks=()=>{};renderHistoryList=()=>{};
    focusStudioInput=()=>{};syncHistoryEditUi=()=>{};updateResultStream=()=>{};
    refreshTasks=async()=>{};reconcileActiveTask=async()=>{};syncStudioModel=async()=>{};
    saveStudioPrefs=async()=>true;startStudioPolling=()=>{};
    renderResultPanel=task=>{studio.lastResult=task;globalThis.recordRender(task);};
    setStudioPane=pane=>{studio.pane=pane;globalThis.recordPane(pane);};
    setResultMode=mode=>{studio.resultMode=mode;};
    renderImageQueue=()=>{updateStudioImageConfirm();};
    globalThis.p={studio,setPromptSource,selectStudioEntry,pickStudioTask,retryStudioTask,submitGenerate,submitFollowup,
      openStudioImageDialog,confirmStudioImages,addStudioImageFiles,uploadStudioImage,discardStudioImageDialog,
      prepareContentFollowup,getContentFollowup,clearContentFollowup,createContentSkillPicker,
      restoreStudioSelection,refreshStudio,closeStudioView,setStudioPage,renderContentThread,openHistoryList,
      model:actualSyncStudioModel,refreshTasks:actualRefreshTasks,
      useReconcile(){reconcileActiveTask=actualReconcile;},useModels(){syncStudioModel=actualSyncStudioModel;},
      useImageRender(){renderImageQueue=actualImageRender;},
    };
  `,Object.assign(context,{recordRender:task=>renders.push(task),recordPane:pane=>panes.push(pane)}));
  const p=context.p;
  Object.assign(p.studio,{open:true,generationInitialized:true,modelName:'m1',providerKey:'p1',promptSource:'template'});
  $('#studioProviderSelect').append(Object.assign(element('option'),{value:'p1'}),Object.assign(element('option'),{value:'p2'}));
  $('#studioProviderSelect').value='p1';
  $('#studioModelSelect').append(Object.assign(element('option'),{value:'m1'}));
  $('#studioModelSelect').value='m1';
  $('#studioUserRequest').value='需求';
  return {p,context,$,requests,toasts,renders,panes,xhr,store};
}
const task=(id,source='template',status='done')=>({id,status,result:id+'正文',input:{prompt_source:source,model_name:'m1'}});
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行真实前端函数")
class StudioContentUiTests(unittest.TestCase):
    def run_js(self, body):
        script = HARNESS + "\n(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"
        result = subprocess.run([shutil.which("node"), "-"], input=script, cwd=ROOT,
                                text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_history_source_switch_never_waits_then_steals_new_selection(self):
        self.run_js(r"""
const f=fixture();f.p.useReconcile();
f.p.studio.sourceDrafts.skill={activeTaskId:'old-skill',lastResult:task('old-skill','skill')};
const first=f.p.pickStudioTask('A');f.requests[0].resolve({task:task('A','skill')});await tick();
const second=f.p.pickStudioTask('B');f.requests.find(r=>r.url.endsWith('/B')).resolve({task:task('B','skill')});await second;
for(const request of f.requests.filter(r=>r.url.endsWith('/old-skill')))request.resolve({task:task('old-skill','skill')});
await first;assert.equal(f.p.studio.activeTaskId,'B');assert.equal(f.p.studio.lastResult.id,'B');
assert.equal(f.requests.filter(r=>r.url.endsWith('/old-skill')).length,0,'历史选择不得恢复上一条分页任务');
const late=f.p.pickStudioTask('late');f.p.setPromptSource('template');
f.requests.find(r=>r.url.endsWith('/late')).resolve({task:task('late','skill')});await late;
assert.equal(f.p.studio.promptSource,'template');assert.notEqual(f.p.studio.activeTaskId,'late');
""")

    def test_template_loading_and_restore_guard_block_stale_submission(self):
        self.run_js(r"""
const f=fixture();f.p.studio.promptSource='skill';
const first=f.p.selectStudioEntry('builtin','A');assert.equal(f.requests[0].url,'/api/studio/catalog/A');
assert.equal(f.p.studio.detailLoading,true);await f.p.submitGenerate();assert.equal(f.requests.length,1);
const second=f.p.selectStudioEntry('builtin','B');
f.requests[1].resolve({item:{id:'B'},body:'B正文'});await second;
f.requests[0].resolve({item:{id:'A'},body:'A正文'});await first;
assert.equal(f.p.studio.itemId,'B');assert.equal(f.p.studio.body,'B正文');assert.equal(f.p.studio.detailLoading,false);
const third=f.p.selectStudioEntry('builtin','C');f.p.setPromptSource('skill');
f.requests[2].resolve({item:{id:'C'},body:'C正文'});await third;
assert.equal(f.p.studio.promptSource,'skill');assert.notEqual(f.p.studio.body,'C正文');
""")

    def test_models_are_owned_by_provider_and_loading_cannot_submit(self):
        self.run_js(r"""
const f=fixture();f.p.studio.modelOptionsKey='p1';f.p.studio.providerKey='p2';
const load=f.p.model();assert.equal(f.requests.length,1,'同名模型不能复用另一供应商目录');
assert.equal(f.p.studio.modelLoading,true);assert.equal(f.$('#studioModelSelect').options.length,0);
await f.p.submitGenerate();assert.equal(f.requests.length,1);
f.p.studio.providerKey='p1';f.p.studio.modelName='';const newer=f.p.model();
f.requests[1].resolve({models:['new-first','new-second']});await newer;
f.requests[0].resolve({models:['stale-p2']});await load;
assert.equal(f.$('#studioModelSelect').value,'new-first');assert.equal(f.p.studio.modelName,'new-first');
assert.equal(f.p.studio.modelOptionsKey,'p1');assert.equal(f.p.studio.modelLoading,false);
assert.equal(f.$('#studioModelSelect').disabled,false);
""")

    def test_retry_and_generate_late_results_do_not_change_current_or_saved_drafts(self):
        self.run_js(r"""
const f=fixture();f.p.studio.activeTaskId='failed';
const retry=f.p.retryStudioTask();await f.p.retryStudioTask();assert.equal(f.requests.length,1,'双击不重复重试');
const pick=f.p.pickStudioTask('new');f.requests[1].resolve({task:task('new')});await pick;
f.requests[0].resolve({task:task('retry-created','template','queued')});await retry;
assert.equal(f.p.studio.activeTaskId,'new');assert.equal(f.p.studio.retryBusy,false);
const submit=f.p.submitGenerate();const submission=f.requests.find(r=>r.url==='/api/studio/generate');
f.p.setPromptSource('skill');const saved=JSON.stringify(f.p.studio.sourceDrafts.template);
submission.resolve({task:task('late-submit','template','queued')});await submit;
assert.equal(JSON.stringify(f.p.studio.sourceDrafts.template),saved,'迟到提交只能刷新记录列表');
assert.equal(f.p.studio.promptSource,'skill');assert.notEqual(f.p.studio.activeTaskId,'late-submit');
""")

    def test_followup_receipt_survives_network_error_reload_and_cleans_only_its_key(self):
        self.run_js(r"""
const shared=storage(),f=fixture(shared);f.p.studio.lastResult=task('parent');f.p.studio.activeTaskId='parent';
f.$('#studioFollowupText').value='再写一点';f.p.studio.followupImages=[{path:'/managed/a.png',name:'A'}];
const first=f.p.submitFollowup();const firstRequest=f.requests[0];firstRequest.reject(new Error('连接断开'));await first;
const key=firstRequest.options.body.request_key;assert.ok(key);assert.equal(f.$('#studioFollowupText').value,'再写一点');
const g=fixture(shared);const pick=g.p.pickStudioTask('parent');g.requests[0].resolve({task:task('parent')});await pick;
assert.equal(g.$('#studioFollowupText').value,'再写一点');assert.equal(g.p.studio.followupImages.length,1);
const retry=g.p.submitFollowup();const request=g.requests[1];assert.equal(request.options.body.request_key,key);
request.resolve({task:task('accepted','template','queued')});await retry;
assert.equal(g.p.getContentFollowup('parent'),null);assert.equal(g.$('#studioFollowupText').value,'');
const old=g.p.prepareContentFollowup('parent','旧',[]),newer=g.p.prepareContentFollowup('parent','新',[]);
assert.equal(g.p.prepareContentFollowup('parent','旧',[]).request_key,old.request_key,'改稿再切回仍保留旧请求幂等');
g.p.clearContentFollowup('parent',old.request_key);assert.equal(g.p.getContentFollowup('parent').request_key,newer.request_key);
""")

    def test_followup_preserves_new_edits_and_late_response_cannot_override_task(self):
        self.run_js(r"""
const f=fixture();f.p.studio.lastResult=task('parent');f.p.studio.activeTaskId='parent';f.$('#studioFollowupText').value='旧输入';
f.p.studio.followupImages=[{path:'/a.png'}];const first=f.p.submitFollowup();
f.$('#studioFollowupText').value='新草稿';f.p.studio.followupImages=[{path:'/b.png'}];
f.requests[0].resolve({task:task('accepted','template','queued')});await first;
assert.equal(f.$('#studioFollowupText').value,'新草稿');assert.equal(f.p.studio.followupImages[0].path,'/b.png');
f.p.studio.lastResult=task('parent');f.p.studio.activeTaskId='parent';const second=f.p.submitFollowup();
const pick=f.p.pickStudioTask('elsewhere');f.requests[2].resolve({task:task('elsewhere')});await pick;
const saved=JSON.stringify(f.p.studio.sourceDrafts);f.requests[1].resolve({task:task('late-child','template','queued')});await second;
assert.equal(f.p.studio.activeTaskId,'elsewhere');assert.equal(JSON.stringify(f.p.studio.sourceDrafts),saved);
assert.equal(f.p.getContentFollowup('parent').request_key,f.requests[1].options.body.request_key,'未被当前视图消费的成功回包仍保留幂等key');
const denied=fixture({getItem(){return null;},setItem(){throw new Error('storage blocked');}});
denied.p.studio.lastResult=task('p');denied.$('#studioFollowupText').value='x';await denied.p.submitFollowup();
assert.equal(denied.requests.length,0,'不能保存幂等标识时不得先发送');
""")

    def test_image_upload_generation_owner_and_deleted_queue_entries(self):
        self.run_js(r"""
const f=fixture();f.p.openStudioImageDialog();
const upload=f.p.addStudioImageFiles([{name:'one.png',type:'image/png',size:20},{name:'two.png',type:'image/png',size:20}]);
assert.equal(f.xhr.length,1);assert.equal(f.p.studio.imageUploading,true);
await f.p.submitGenerate();assert.equal(f.requests.length,0);
f.p.setPromptSource('skill');f.p.openStudioImageDialog('followup');
f.p.studio.imageQueue=[{key:'new',status:'done',path:'/new.png',name:'new'}];
f.xhr[0].done('/old.png');await upload;
assert.equal(f.xhr.length,1,'旧串行队列关闭后不得继续上传第二张');
assert.equal(f.p.studio.imageQueue[0].path,'/new.png');
f.p.confirmStudioImages();assert.equal(f.p.studio.followupImages[0].path,'/new.png');assert.equal(f.p.studio.images.length,0);
f.p.studio.activeTaskId='A';f.p.openStudioImageDialog('followup');f.p.studio.activeTaskId='B';
f.p.studio.imageQueue=[{status:'done',path:'/wrong.png'}];f.p.confirmStudioImages();
assert.equal(f.p.studio.followupImages[0].path,'/new.png','换任务后旧弹窗不可确认到新任务');
""")

    def test_skill_picker_refresh_preserves_latest_selection_and_destroyed_clicks(self):
        self.run_js(r"""
const f=fixture(),container=element(),changes=[];const picker=f.p.createContentSkillPicker(container,{onChange:ids=>changes.push(ids)});
const root=container.children[0],options=root.children[2];
f.requests[0].resolve({skills:[{id:'A',name:'Alpha'},{id:'B',name:'Beta'}]});await tick();
const oldButton=options.children[0];oldButton.click();oldButton.click();assert.equal(picker.getSelected().length,1);
const refresh=picker.refresh();picker.setSelected(['B']);f.requests[1].resolve({skills:[{id:'A',name:'Alpha'},{id:'B',name:'Beta'}]});await refresh;
assert.equal(JSON.stringify(picker.getSelected()),JSON.stringify(['B']));
const third=picker.refresh(),fourth=picker.refresh();f.requests[3].resolve({skills:[{id:'D',name:'Delta'}]});await fourth;
f.requests[2].resolve({skills:[{id:'C',name:'Old'}]});await third;
assert.equal(options.children[0].textContent,'Delta');const lateButton=options.children[0];picker.destroy();lateButton.click();
assert.equal(JSON.stringify(picker.getSelected()),JSON.stringify(['B']));
""")

    def test_template_response_cannot_steal_history_pane_or_closed_view(self):
        self.run_js(r"""
const f=fixture();const select=f.p.selectStudioEntry('builtin','A');
const pick=f.p.pickStudioTask('history');f.requests[1].resolve({task:task('history')});await pick;
assert.equal(f.p.studio.pane,'result');f.requests[0].resolve({item:{id:'A'},body:'正文'});await select;
assert.equal(f.p.studio.pane,'result','模板迟到加载不能抢回表单');assert.equal(f.p.studio.detailLoading,false);
const closed=f.p.selectStudioEntry('builtin','closed');f.p.closeStudioView();
f.requests[2].resolve({item:{id:'closed'},body:'不该加载'});await closed;
assert.notEqual(f.p.studio.body,'不该加载');assert.equal(f.p.studio.detailLoading,false);
""")

    def test_history_navigation_during_submit_keeps_history_visible(self):
        self.run_js(r"""
const f=fixture();const submit=f.p.submitGenerate();const list=f.p.openHistoryList();
f.requests[1].resolve({tasks:[task('old')]});await list;
f.requests[0].resolve({task:task('created','template','queued')});await submit;
assert.equal(f.p.studio.resultMode,'history');assert.notEqual(f.p.studio.activeTaskId,'created');
""")

    def test_task_list_refresh_and_view_close_reject_late_responses(self):
        self.run_js(r"""
const f=fixture();const first=f.p.refreshTasks(),second=f.p.refreshTasks();
f.requests[1].resolve({tasks:[task('new')]});await second;f.requests[0].resolve({tasks:[task('old')]});await first;
assert.equal(f.p.studio.tasks[0].id,'new');
const submit=f.p.submitGenerate();f.p.closeStudioView();f.requests[2].resolve({task:task('late','template','queued')});await submit;
assert.notEqual(f.p.studio.activeTaskId,'late');assert.equal(f.p.studio.open,false);
""")


class StudioWorkbenchLayoutGuardTests(unittest.TestCase):
    """提词库三处界面修复 + 追问面板收口的静态守门（运行时由 verify 探针/冒烟覆盖）。"""

    def setUp(self):
        self.source = (ROOT / "public/js/22-studio.js").read_text(encoding="utf-8")
        self.css = (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_followup_collapses_on_template_or_source_switch(self):
        self.assertIn("function collapseStudioFollowup()", self.source)
        select = self.source[self.source.index("async function selectStudioEntry"):
                              self.source.index("async function loadCatalogEntry")]
        self.assertIn("collapseStudioFollowup()", select, "换模板后要收起追问面板")
        prompt = self.source[self.source.index("function setPromptSource"):
                             self.source.index("function renderSkillHistory")]
        self.assertIn("collapseStudioFollowup()", prompt, "换来源后要收起追问面板")
        render = self.source[self.source.index("function renderResultPanel"):
                             self.source.index("async function refreshTasks")]
        self.assertIn("syncFollowupVisibility()", render, "结果面板仍按任务状态开合")

    def test_icon_button_and_long_skill_list_keep_their_geometry(self):
        # 34px 图标按钮减去文字按钮的 24px 内边距 ⇒ 16px 图标被压成 8px（时钟只剩半个）。
        self.assertIn(":is(.studio-view, .super-agent-view) .studio-icon-button { padding: 0; }", self.css)
        # 列布局 + max-height 会把 Skill 条目压到 19px，nowrap 文字互相重叠。
        self.assertIn(".content-skill-options > * { flex: none; }", self.css)
        # 结果卡的 ⋯ 菜单右对齐会伸到卡片外，被 overflow:hidden 裁掉。
        self.assertIn("#studioPageLibrary .studio-result-menu .wb-media-menu-panel { left: 0; right: auto; }", self.css)

    def test_request_keys_do_not_require_secure_context(self):
        """局域网 http 不是安全上下文，`crypto.randomUUID` 不存在；请求标识必须走 randomUuid()。"""
        for path in sorted((ROOT / "public/js").glob("*.js")):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "crypto.randomUUID(", source,
                f"{path.name} 直接用 crypto.randomUUID()：手机用 http 访问会抛 TypeError",
            )
        core = (ROOT / "public/js/01-core.js").read_text(encoding="utf-8")
        self.assertIn("export function randomUuid()", core)
        self.assertIn("getRandomValues", core)


if __name__ == "__main__":
    unittest.main()
