# -*- coding: utf-8 -*-
"""任务总管入口、控制面、创建弹窗和移动端样式契约。"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SuperAgentUITests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_sidebar_entry_and_independent_view_exist_once(self) -> None:
        html = self._read("public/index.html")
        self.assertEqual(html.count('id="openSuperAgent"'), 1)
        self.assertEqual(html.count('id="superAgentView"'), 1)
        sidebar = html[html.index('<aside class="sidebar"'):html.index("</aside>")]
        self.assertIn('id="openSuperAgent"', sidebar)
        view = html[html.index('<section class="super-agent-view"'):html.index('<section class="composer-wrap">')]
        for marker in ("superAgentThreadList", "superAgentDetail", "superAgentNodeList", "superAgentEventList", "superAgentApprovalList", "superAgentArtifactList"):
            self.assertIn(f'id="{marker}"', view)

    def test_legacy_tasks_dialog_is_preserved_with_explicit_handoff(self) -> None:
        html = self._read("public/index.html")
        dialog = html[html.index('<dialog id="tasksDialog"'):html.index("</dialog>", html.index('<dialog id="tasksDialog"'))]
        self.assertIn('id="taskList"', dialog)
        self.assertIn('id="openTaskManager"', dialog)
        self.assertNotIn("创建任务", dialog)

    def test_frontend_control_api_contract(self) -> None:
        source = self._read("public/js/28-super-agent.js")
        self.assertIn('api("/api/super-agent/threads")', source)
        self.assertIn("/nodes`", source)
        self.assertIn("/events", source)
        self.assertIn("/artifacts`", source)
        self.assertIn("/approvals`", source)
        self.assertGreaterEqual(source.count('method: "POST"'), 3)
        self.assertIn('body: { action, node_id: nodeId, request_id:', source)
        self.assertIn('body: { status, request_id:', source)
        self.assertIn('start_request: startRequest', source)
        self.assertIn('/api/providers/models', source)
        self.assertIn('provider_id: providerId', source)
        self.assertIn('model_key: providerKey', source)
        self.assertIn('该供应商暂无可用模型', source)
        self.assertIn('state.providerModelCatalogs[providerKey]', source)
        self.assertNotIn('catalogs: new Map()', source)
        self.assertIn('superAgentCreateModelRefresh', source)
        self.assertIn('requestId !== createModelState.requestId', source)
        self.assertIn('没有独立 API 设置', source)
        self.assertNotIn('superAgentCreateModelName', source)
        self.assertNotIn('superAgentCreateModelKey', source)
        self.assertIn('openStudioView();', source)
        self.assertIn('setStudioPage(domain);', source)
        self.assertNotIn('window.prompt(', source)
        html = self._read("public/index.html")
        for marker in ("superAgentCreateDialog", "superAgentCreateBookId", "superAgentCreateStage", "superAgentCreateProvider", "superAgentCreateModel", "superAgentCreateSubmit"):
            self.assertIn(marker, html)
        self.assertNotIn('id="superAgentCreateModelName"', html)
        self.assertNotIn('id="superAgentCreateModelKey"', html)
        self.assertIn('class="writer-dialog-form super-agent-create-form"', html)
        self.assertIn('class="writer-manage-head"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertNotIn("只读查看", html)
        self.assertIn("任务已结束，无需取消", source)
        self.assertIn("操作失败：", source)
        self.assertIn('super-agent-create-dialog', self._read("public/styles.css"))
        self.assertIn('height: 100dvh', self._read("public/styles.css"))
        self.assertIn('bindSuperAgentEvents()', self._read("public/js/15-bind-events.js"))

    def test_app_view_contract_and_no_workbench_page_changes(self) -> None:
        core = self._read("public/js/01-core.js")
        self.assertIn("appView: 'chat'", core)
        module = self._read("public/js/28-super-agent.js")
        self.assertIn('setAppView(next)', module)
        self.assertIn('setAppView(view)', core)
        for name in ("23-labeler.js", "24-comfyui.js", "25-novel.js", "27-writer.js"):
            source = self._read(f"public/js/{name}")
            self.assertNotIn("superAgentView", source)
            self.assertNotIn("/api/super-agent", source)
        bind = self._read("public/js/15-bind-events.js")
        self.assertEqual(bind.count("#openChatMode"), 1, "顶层 Chat 入口只应在组合根绑定一次")

    def test_stage_chain_projection_and_cursor_contract(self) -> None:
        source = self._read("public/js/28-super-agent.js")
        html = self._read("public/index.html")
        css = self._read("public/styles.css")
        for field in ("stage_key", "sequence", "chain_version", "current_node_id"):
            self.assertIn(field, source)
        for status in ("blocked", "ready", "running", "waiting_user", "failed"):
            self.assertIn(f"status-{status}", css)
        self.assertIn("superAgentStageChain", html)
        self.assertIn("superAgentChainMeta", html)
        self.assertIn("?after=", source)
        self.assertIn("mergeEvents", source)
        self.assertIn("data-node-id", source)
        self.assertIn("data-super-action", source)
        self.assertIn("data-super-approval", source)
        self.assertIn("data-super-return-domain", source)

    def test_mobile_layout_is_single_column_and_wrap_safe(self) -> None:
        css = self._read("public/styles.css")
        self.assertIn(".super-agent-columns { display: flex; flex-direction: column;", css)
        self.assertIn("min-width: 0", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertRegex(css, r"@media \(max-width: 760px\)[\s\S]*?\.super-agent-side-panel")

    def test_single_render_target_and_independent_navigation(self) -> None:
        source = self._read("public/js/28-super-agent.js")
        html = self._read("public/index.html")
        css = self._read("public/styles.css")
        self.assertNotIn('$("#superAgentStageChain").innerHTML', source)
        self.assertEqual(source.count('$("#superAgentNodeList")?.addEventListener("click"'), 1)
        self.assertNotIn("setLeftSidebarCollapsed", source)
        self.assertNotIn("superAgentCreateInstruction", html + source)
        self.assertIn('id="superAgentAppNav"', html)
        self.assertIn('data-super-view="chat"', html)
        self.assertIn('data-super-view="studio"', html)
        self.assertIn('body[data-app-view="super-agent"] .app-shell.file-panel-open { grid-template-columns: minmax(0, 1fr); }', css)
        self.assertIn('body[data-app-view="super-agent"] .sidebar,', css)
        self.assertIn('id="superAgentCreateModelOptions"', html)
        self.assertIn('id="superAgentCreateMixedOptions"', html)
        self.assertIn('message: goal', source)
        self.assertIn('instruction: goal', source)
        self.assertIn('detail.chat_confirmations', source)
        self.assertIn('executor_kind === "subagent"', source)
        self.assertIn('data-super-tool-decision', source)
        self.assertIn('body: { run_id: runId, confirm_id: confirmId }', source)

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the form behavior test")
    def test_single_goal_payload_and_filters_execute_in_javascript(self) -> None:
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const source = fs.readFileSync('public/js/28-super-agent.js', 'utf8')
  .replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
const elements = new Map();
const $ = selector => {
  if (!elements.has(selector)) elements.set(selector, { value: '', checked: false, textContent: '', hidden: false });
  return elements.get(selector);
};
const state = { bootstrap: { model_profiles: [{ id: 'provider', model_key: 'test' }] },
  providerModelCatalogs: { test: ['local-model'] } };
const context = { $, state, console, escapeHtml: value => String(value), globalThis, setTimeout, clearTimeout };
vm.runInNewContext(source + `
(async () => {
  const captured = [];
  submitCreateBody = async body => captured.push(body);
  createState.conversations = [{ id: 'conversation', title: '测试会话' }];
  $('#superAgentCreateProvider').value = 'test';
  $('#superAgentCreateModel').value = 'local-model';
  $('#superAgentCreateConversation').value = 'conversation';
  $('#superAgentCreateGoal').value = '  整理现有资料  ';
  for (const domain of ['chat', 'subagent']) {
    $('#superAgentCreateDomain').value = domain;
    await createDomainThread(null);
  }
  if (captured.length !== 2) throw new Error('Chat and subassistant must both create');
  if (captured[0].goal !== '整理现有资料' || captured[0].start_request.message !== captured[0].goal) throw new Error('Chat must reuse the single goal');
  if (captured[1].start_request.instruction !== captured[1].goal) throw new Error('Subassistant must reuse the single goal');
  if (captured.some(body => body.autostart)) throw new Error('Creation must not start by default');
  $('#superAgentCreateGoal').value = '  ';
  await createDomainThread(null);
  if (captured.length !== 2 || !$('#superAgentCreateError').textContent.includes('你想完成什么')) throw new Error('Empty instruction must not submit');
  viewState.threads = [{thread_id: 'a', status: 'active'}, {thread_id: 'b', status: 'waiting_user'}, {thread_id: 'c', status: 'completed'}];
  viewState.filter = 'waiting';
  if (filteredThreads().map(row => row.thread_id).join() !== 'b') throw new Error('Waiting filter');
  viewState.filter = 'completed';
  if (filteredThreads().map(row => row.thread_id).join() !== 'c') throw new Error('Completed filter');
  viewState.filter = 'all';
  if (filteredThreads().length !== 3) throw new Error('All filter');
})().catch(error => { console.error(error); process.exitCode = 1; });
`, { ...context, process });
"""
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the form behavior test")
    def test_supervised_form_limits_and_review_rendering_execute_in_javascript(self) -> None:
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync('public/js/28-super-agent.js', 'utf8').replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
const elements = new Map();
const $ = key => { if (!elements.has(key)) elements.set(key, {value:'', checked:false, textContent:'', hidden:false, innerHTML:''}); return elements.get(key); };
const state = {bootstrap:{model_profiles:[{id:'provider', model_key:'test'}]}, providerModelCatalogs:{test:['local']}};
vm.runInNewContext(source + `
(async () => {
  const captured = [];
  submitCreateBody = async body => captured.push(body);
  createState.conversations = [{id:'conversation', title:'已有会话'}];
  $('#superAgentCreateDomain').value = 'chat';
  $('#superAgentCreateProvider').value = 'test';
  $('#superAgentCreateModel').value = 'local';
  $('#superAgentCreateConversation').value = 'conversation';
  $('#superAgentCreateGoal').value = '修复导出';
  $('#superAgentCreateExecutionMode').value = 'supervised';
  $('#superAgentCreateContinuations').value = '3';
  $('#superAgentCreateWindow').value = '30';
  await createDomainThread(null);
  assert.equal(captured.length, 0);
  assert.match($('#superAgentCreateError').textContent, /完成标准/);
  $('#superAgentCreateCriteria').value = '按钮可用\\n  文件内容正确  \\n';
  $('#superAgentCreateContinuations').value = '6';
  await createDomainThread(null);
  assert.equal(captured.length, 0);
  $('#superAgentCreateContinuations').value = '3';
  $('#superAgentCreateWindow').value = '0';
  await createDomainThread(null);
  assert.equal(captured.length, 0);
  $('#superAgentCreateWindow').value = '30';
  await createDomainThread(null, {addStep:true});
  assert.equal(captured.length, 0);
  await createDomainThread(null);
  assert.equal(captured.length, 1);
  assert.equal(captured[0].execution_mode, 'supervised');
  assert.equal(captured[0].autostart, false);
  assert.equal(JSON.stringify(captured[0].supervision), JSON.stringify({completion_criteria:['按钮可用','文件内容正确'],max_continuations:3,window_minutes:30}));
  $('#superAgentCreateExecutionMode').value = 'single';
  await createDomainThread(null);
  assert.equal(captured.length, 2);
  assert.equal(captured[1].supervision, undefined);
  renderSupervision({supervision:{phase:'reviewing',round:2,max_rounds:4,remaining_continuations:2,review_count:1,review_started_at:Date.now()-3000,criteria:['检查文件'],rounds:[{round:1,run_id:'run-one',review:{decision:'continue',summary:'缺少检查',missing_items:['文件尚未检查'],next_instruction:'<script>请检查</script>'}}]}});
  const output = $('#superAgentSupervision').innerHTML;
  assert.match(output, /Token 用量：未知/);
  assert.match(output, /已等待约 3 秒/);
  assert.match(output, /缺少检查/);
  assert.match(output, /&lt;script&gt;/);
  assert.ok(!output.includes('<script>'));
  renderSupervision({});
  assert.equal($('#superAgentSupervision').hidden, true);
})().catch(error => {console.error(error);process.exitCode=1;});
`, {$,state,assert,process,console,globalThis,setTimeout,clearTimeout,escapeHtml:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')});
"""
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute UI contracts")
    def test_capabilities_confirmations_and_draft_modes_execute_in_javascript(self) -> None:
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('public/js/28-super-agent.js','utf8').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
const elements=new Map();
const $=key=>{if(!elements.has(key))elements.set(key,{value:'',checked:false,textContent:'',hidden:false,innerHTML:'',dataset:{},querySelectorAll:()=>[],insertAdjacentHTML(where,html){this.innerHTML+=html;}});return elements.get(key);};
vm.runInNewContext(source+`
  $('#superAgentCreateDomain').value='job';
  $('#superAgentCreateAutostart').checked=true;
  renderCreateMode();
  assert.equal($('#superAgentCreateAutostart').checked,true);
  assert.equal($('#superAgentCreateAutostart').disabled,true);
  mixedDraft.push({key:'split',domain:'writer',book_id:'book',stage:'split',title:'书',model_name:'local',start_request:{part_no:1,from_no:2,want_count:2}});
  renderMixedDraft();
  assert.equal($('#superAgentCreateAutostart').disabled,false);
  assert.match($('#superAgentCreateSteps').innerHTML,/拆章.*第 1 部，第 2–3 章.*local/);
  assert.equal(draftProvidesChapter('book',2),true);
  assert.equal(draftProvidesChapter('other',2),false);
  assert.equal(draftProvidesChapter('book',4),false);
  assert.equal(draftProvidesChapter('book',2,0),false);
  $('#superAgentCreateDomain').value='chat';
  $('#superAgentCreateExecutionMode').value='supervised';
  renderCreateMode();
  assert.equal($('#superAgentCreateMixedOptions').hidden,false);
  assert.equal($('#superAgentCreateAddStep').disabled,true);
  assert.match($('#superAgentCreateRuntimeHint').textContent,/已保留 1 个步骤/);
  $('#superAgentCreateExecutionMode').value='single';renderCreateMode();
  assert.equal(mixedDraft.length,1);assert.equal($('#superAgentCreateAddStep').disabled,false);
  viewState.detail={status:'needs_recovery',linked_conversation_ids:[],chat_confirmations:[{executor_kind:'subagent',confirm_id:'confirm',run_id:'job',status:'pending',parameters:{path:'<safe>'},action_source:'native'}]};
  viewState.nodes=[{node_id:'node',status:'interrupted',executor_type:'subagent',executor_id:'job',capabilities:{retry:true,resume:false,start:false,cancel:false}}];
  renderDetail();
  assert.ok(!$('#superAgentNodeList').innerHTML.includes('data-super-action="resume"'));
  assert.ok($('#superAgentNodeList').innerHTML.includes('data-super-action="retry"'));
  assert.ok($('#superAgentNodeList').innerHTML.includes('data-super-result="node"'));
  const card=$('#superAgentChatConfirmationList').innerHTML;
  assert.match(card,/data-super-tool-decision="confirm"/);assert.match(card,/&lt;safe&gt;/);
  assert.ok(!card.includes('data-super-conversation'));
  viewState.detail.chat_confirmations[0].status='processing';renderDetail();
  assert.ok(!$('#superAgentChatConfirmationList').innerHTML.includes('data-super-tool-decision'));
  confirmationNotices.set('',{confirmId:'confirm',runId:'job',message:'迟到审批已失效'});
  viewState.detail.chat_confirmations[0].status='pending';renderDetail();
  assert.ok(!$('#superAgentChatConfirmationList').innerHTML.includes('data-super-tool-decision'));
  assert.match($('#superAgentChatConfirmationList').innerHTML,/迟到审批已失效/);
  renderSupervision({supervision:{phase:'waiting_user',can_accept_completion:false,criteria:[],rounds:[]}});
  assert.ok(!$('#superAgentSupervision').innerHTML.includes('data-super-accept-completion'));
  renderSupervision({supervision:{phase:'waiting_user',can_accept_completion:true,criteria:[],rounds:[],token_usage:{total_tokens:null,known_tokens:28,has_unknown:true}}});
  assert.match($('#superAgentSupervision').innerHTML,/data-super-accept-completion/);
  assert.match($('#superAgentSupervision').innerHTML,/已知 28 tokens，部分用量未知/);
`,{$,state:{conversations:[]},assert,process,console,globalThis,setTimeout,clearTimeout,escapeHtml:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')});
"""
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
