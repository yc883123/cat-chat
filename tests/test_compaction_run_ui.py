"""压缩轮首/循环共用一次尝试，成功圆环采用新估算且不改计费。"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from naiba.run.chat import ConversationRunMixin
from naiba.skills.agent import SkillAgent

ROOT = Path(__file__).resolve().parents[1]


class CompactionRunAttemptTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"id": "input", "role": "user", "content": "当前问题", "metadata": {}}]
        self.mixin = ConversationRunMixin()
        self.config = SimpleNamespace(data={"auto_compaction": {"mode": "auto", "threshold_percent": 30}},
                                      reasoning_replay_options=lambda: {}, image_encode_cache_options=lambda: {})
        self.mixin.app = SimpleNamespace(config=self.config,
            tool_registry=SimpleNamespace(schemas=lambda: []),
            storage=SimpleNamespace(get_conversation=lambda _: {"messages": self.rows}))
        self.run = {"conversation_messages": self.rows, "input_message_id": "input"}
        self.events = []
        self.profile = {"kind": "online", "context_window": 10000, "context_window_source": "configured"}
        fit = mock.patch.object(SkillAgent, "_context_fits", return_value=(True, 10000, 6000, 9000))
        self.addCleanup(fit.stop)
        self.fit = fit.start()
        compact = mock.patch("naiba.core.compaction.compact_conversation",
                             return_value={"compacted": False, "reason": "bad_summary"})
        self.addCleanup(compact.stop)
        self.compact = compact.start()

    def start(self, **overrides):
        kwargs = dict(run=self.run, snapshot={"allowed_tools": []}, conversation_id="c1", conv_kind="main",
            frozen_conversation={}, history=[{"role": "user", "content": "当前问题"}], profile=self.profile,
            effective="当前问题", model_key="online:fixture", model_name="fixture", event=self.events.append,
            cancel_event=threading.Event())
        kwargs.update(overrides)
        return self.mixin._maybe_compact_context(**kwargs)

    def loop(self, **overrides):
        kwargs = dict(run=self.run, snapshot={"allowed_tools": []}, conversation_id="c1", conv_kind="main",
            frozen_conversation={}, messages=[{"role": "system", "content": "rules"},
                {"role": "user", "content": "当前问题"}, {"role": "tool", "content": "tool result"}],
            trace_start=2, profile=self.profile, model_key="online:fixture", model_name="fixture",
            pdf_tools=True, video_tools=True, limit=10000, budget=9000, used=6000,
            event=self.events.append, cancel_event=threading.Event())
        kwargs.update(overrides)
        return self.mixin._compact_agent_context(**kwargs)

    def test_failed_turn_start_is_not_retried_by_tool_loop(self):
        self.assertEqual(self.start(), {})
        self.assertTrue(self.run["_compaction_attempted"])
        self.assertEqual(self.loop(), {})
        self.assertEqual(self.start(), {})
        self.compact.assert_called_once()
        self.assertEqual([e["action"] for e in self.events], ["started", "failed"])

    def test_exception_attempt_remains_consumed_for_this_run(self):
        self.compact.side_effect = RuntimeError("failed")
        with self.assertRaisesRegex(RuntimeError, "failed"):
            self.start()
        self.assertEqual(self.loop(), {})
        self.compact.assert_called_once()

    def test_skipped_checks_leave_attempt_available_and_new_run_gets_its_own_attempt(self):
        self.fit.return_value = (True, 10000, 1, 9000)
        self.assertEqual(self.start(), {})
        self.assertNotIn("_compaction_attempted", self.run)
        self.fit.return_value = (True, 10000, 6000, 9000)
        self.start()
        self.start(run={"conversation_messages": self.rows})
        self.assertEqual(self.compact.call_count, 2)

    def test_success_waits_for_complete_request_before_reporting_context(self):
        self.compact.return_value = {"compacted": True, "context_tokens_after": 1200,
                                    "freed_est": 5, "region_messages": 8}
        self.assertTrue(self.start()["rebuilt"])
        payload = self.events[-1]
        self.assertNotIn("context_tokens", payload, "纯历史估算不代表完整请求占用")
        self.assertEqual(payload["model_key"], "online:fixture")
        self.assertEqual(payload["context_limit_source"], "configured")
        self.assertEqual(payload["used"], 6000)
        self.assertEqual(self.loop(), {})
        self.compact.assert_called_once()

    def test_loop_defers_estimate_until_actual_request_is_rebuilt(self):
        self.compact.return_value = {"compacted": True, "context_tokens_after": 1200}
        result = self.loop()
        self.assertIn("history", result)
        self.assertNotIn("context_tokens", self.events[-1])

    def test_unexpected_loop_failure_redacts_profile_and_header_credentials(self):
        self.profile["api_key"] = "configured-credential-value"
        with mock.patch.object(self.mixin, "_maybe_compact_context", side_effect=RuntimeError(
            "HTTP 401 invalid key configured-credential-value; Bearer header-credential-value",
        )):
            self.assertEqual(self.loop(), {})
        payload = self.events[-1]
        self.assertEqual(payload["reason"], "RuntimeError")
        self.assertIn("HTTP 401 invalid key", payload["error"])
        self.assertNotIn("credential-value", payload["error"])
        self.assertNotIn("credential-value", payload["message"])

    def test_failure_event_keeps_diagnostic_error_and_preserves_context(self):
        self.compact.return_value = {"compacted": False, "reason": "model_error", "error": "unsupported max_tokens"}
        self.start()
        payload = self.events[-1]
        self.assertEqual(payload["error"], "unsupported max_tokens")
        self.assertIn("unsupported max_tokens", payload["message"])
        self.assertNotIn("context_tokens", payload)


class CompactionRequestEstimateTests(unittest.TestCase):
    def run_agent(self, *, pending=False, compact=None, replies=None, output_limit=1024):
        events, requests = [], []
        context = {"context_estimate_pending": pending, "model_key": "online:estimate",
                   "context_limit_source": "configured", "compact_context": compact}
        schemas = [{"name": "echo", "description": "Echo a supplied value",
                    "parameters": {"type": "object", "properties": {
                        "text": {"type": "string", "description": "Detailed parameter guidance. " * 80}}}}]
        registry = SimpleNamespace(
            schemas=lambda: schemas, side_effect=lambda _: False,
            media_declaration=lambda _: {"extract": "none", "policy": "never"},
            execute=lambda *_: (True, "tool result for current question"),
        )
        responses = list(replies or ["done"])

        def complete(profile, messages, options, event):
            requests.append({"messages": copy.deepcopy(messages), "options": copy.deepcopy(options),
                             "estimates": len([e for e in events if e.get("action") == "estimated"])})
            return responses.pop(0)

        agent = SkillAgent(SimpleNamespace(scan=lambda: []), None, complete, None)
        agent.run(
            "current question", [{"role": "user", "content": "current question"}],
            {"kind": "online", "model": "probe", "context_window": 128000},
            {"stream": False, "max_tokens": output_limit}, {"mode": "auto", "skill_ids": []},
            [], "User system requirements. " * 30, ["echo"], events.append,
            tool_registry=registry, run_context=context,
        )
        return events, requests, context

    def assert_complete_estimate(self, estimate, request):
        messages, options = request["messages"], request["options"]
        history = sum(SkillAgent._replay_footprint_tokens(item) for item in messages[1:])
        system = SkillAgent._estimate_content_tokens(messages[0]["content"])
        tools = SkillAgent._estimate_serialized_tokens(options["tools"])
        self.assertGreater(system, 0)
        self.assertGreater(tools, 0)
        self.assertEqual(estimate["context_tokens"], history + system + tools + 512)
        self.assertEqual(estimate["model_key"], "online:estimate")
        self.assertEqual(estimate["context_limit_source"], "configured")
        self.assertEqual(estimate["limit"], 128000)
        self.assertEqual([m["content"] for m in messages if m.get("content") == "current question"],
                         ["current question"], "当前输入不重复计算")

    def test_turn_start_estimate_includes_system_schema_and_framing_before_request(self):
        events, requests, context = self.run_agent(pending=True)
        estimates = [event for event in events if event.get("action") == "estimated"]
        self.assertEqual(len(estimates), 1)
        self.assertEqual(requests[0]["estimates"], 1, "估算应在模型请求前可见")
        self.assertFalse(context["context_estimate_pending"])
        self.assert_complete_estimate(estimates[0], requests[0])

    def test_output_reserve_is_not_reported_as_existing_context(self):
        first, _, _ = self.run_agent(pending=True, output_limit=1024)
        second, _, _ = self.run_agent(pending=True, output_limit=20000)
        first_estimate = next(e["context_tokens"] for e in first if e.get("action") == "estimated")
        second_estimate = next(e["context_tokens"] for e in second if e.get("action") == "estimated")
        self.assertEqual(first_estimate, second_estimate)

    def test_normal_request_has_no_compaction_estimate(self):
        events, _, _ = self.run_agent()
        self.assertFalse(any(e.get("action") == "estimated" for e in events))

    def test_consumed_pending_flag_does_not_emit_again_for_next_tool_request(self):
        action = json.dumps({"type": "tool", "tool": "echo", "arguments": {"text": "value"}})
        events, requests, _ = self.run_agent(pending=True, replies=[action, "done"])
        self.assertEqual(len(requests), 2)
        self.assertEqual([request["estimates"] for request in requests], [1, 1])
        self.assertEqual(len([e for e in events if e.get("action") == "estimated"]), 1)

    def test_loop_compaction_estimates_rebuilt_history_and_pending_tool_trace(self):
        action = json.dumps({"type": "tool", "tool": "echo", "arguments": {"text": "value"}})
        def compact(messages, trace_start, limit, budget, used):
            return {"history": [{"role": "user", "content": "summary"},
                                {"role": "user", "content": "current question"}],
                    "limit": limit, "budget": budget}

        events, requests, context = self.run_agent(compact=compact, replies=[action, "done"])
        estimates = [e for e in events if e.get("action") == "estimated"]
        self.assertEqual([request["estimates"] for request in requests], [0, 1])
        self.assertEqual(len(estimates), 1)
        self.assertFalse(context["context_estimate_pending"])
        self.assert_complete_estimate(estimates[0], requests[1])
        self.assertEqual(len([item for item in requests[1]["messages"] if item["role"] == "tool"]), 1)


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行真实圆环渲染函数")
class CompactionRingUITests(unittest.TestCase):
    def test_compaction_updates_ring_preserves_cost_and_real_usage_replaces_estimate(self):
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const nodes=new Map(),toasts=[],activities=[];
function node(){const classes=new Set();return {textContent:'',title:'',style:{setProperty(k,v){this[k]=v;}},
 classList:{add:v=>classes.add(v),remove:(...v)=>v.forEach(x=>classes.delete(x)),
 toggle(v,on){if(on)classes.add(v);else classes.delete(v);},contains:v=>classes.has(v)}};}
const $=s=>{if(!nodes.has(s))nodes.set(s,node());return nodes.get(s);};
const state={conversationId:'c1',chatBusy:true,contextUsage:null,bootstrap:{model_profiles:[]}};
const context=vm.createContext({state,$,toast:s=>toasts.push(s),selectedProvider:()=>({}),
 maybeWarnContextUsage(){},updateContextComposerLock(){}});
const media=fs.readFileSync('public/js/03-media.js','utf8');
const ring=media.slice(media.indexOf('export function setContextUsage('),media.indexOf('// 上下文提醒阈值'))
 .replace(/^export /gm,'');
const chat=fs.readFileSync('public/js/12-chat-input.js','utf8');
const handlers=chat.slice(chat.indexOf('function clearCompactionIndicator('),chat.indexOf('function handleContextFullEvent('));
vm.runInContext(ring+'\n'+handlers,context);
const old={input_tokens:6000,output_tokens:100,total_tokens:6100,context_tokens:9500,context_limit:10000,
 context_limit_source:'configured',model_key:'online:fixture',cost:{total:1.5}};
context.setContextUsage(old);
assert.equal(state.contextPercent,95);
const eventContext={row:node(),setActivity(value){activities.push(value);}};
context.handleCompactionEvent({action:'started'},eventContext);
context.handleCompactionEvent({action:'failed',message:'failure'},eventContext);
assert.equal(state.contextUsage,old,'失败不能假装降低上下文');
assert.equal(state.contextPercent,95);
context.handleCompactionEvent({action:'compacted',limit:10000,
 context_limit_source:'configured',model_key:'online:fixture',region_messages:8},eventContext);
assert.equal(state.contextUsage.context_pending,true);
assert.equal(state.contextUsage.context_tokens,null);
assert.equal($('#contextPercentLabel').textContent,'上下文 --');
assert.equal(state.contextUsage.cost,old.cost);
const toastCount=toasts.length,activityCount=activities.length;
context.handleCompactionEvent({action:'estimated',context_tokens:2000,limit:10000,
 context_limit_source:'configured',model_key:'online:fixture'},eventContext);
assert.equal(toasts.length,toastCount,'占用估算不重复弹整理完成提示');
assert.equal(activities.length,activityCount,'占用估算不改当前工作状态');
assert.equal(state.contextUsage.context_pending,false);
assert.equal(state.contextPercent,20);
assert.equal($('#contextUsageRing').style['--context-percent'],'20.0');
assert.equal(state.contextUsage.input_tokens,6000);
assert.equal(state.contextUsage.output_tokens,100);
assert.equal(state.contextUsage.total_tokens,6100);
assert.equal(state.contextUsage.cost,old.cost);
assert.ok($('#contextUsageSummary').textContent.includes('估算'));
assert.ok($('#contextPercentLabel').textContent.includes('估算'));
assert.equal($('#contextUsageRing').classList.contains('danger'),false);
context.setContextUsage({...old,context_tokens:2400});
assert.equal(state.contextPercent,24);
assert.equal($('#contextUsageSummary').textContent.includes('估算'),false);
context.handleCompactionEvent({action:'compacted'},eventContext);
assert.equal(state.contextUsage.context_tokens,null,'旧事件没有新估算，不能继续显示旧占用');
assert.equal(state.contextUsage.context_pending,true);
assert.equal(state.contextUsage.cost,old.cost,'等待完整估算也保留真实计费');
assert.equal($('#contextPercentLabel').textContent,'上下文 --');
"""
        result = subprocess.run([shutil.which("node"), "-"], input=script, text=True, encoding="utf-8",
                                cwd=ROOT, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
