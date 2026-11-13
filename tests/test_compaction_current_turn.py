"""首轮长工具任务、截断恢复和完整轨迹投影的故障回归。"""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from naiba.core import compaction
from naiba.core.history import build_model_history
from naiba.core.trace_view import (TraceMessages, complete_tool_boundaries, replay_trace, trace_fingerprint,
                                   source_user_fingerprint, is_summary_message, is_tool_result_message)
from naiba.core.exceptions import TaskCancelled
from naiba.run.chat import ConversationRunMixin
from naiba.skills.agent import SkillAgent
from naiba.storage.store import ChatStorage
from tests.test_agent_compaction_trace import _Catalog, _Registry, _action
from tests.test_compaction import GOOD_SUMMARY


class _Model:
    last_usage = {}
    last_reasoning = ""
    last_reasoning_id = ""
    last_finish_reason = "stop"

    def __init__(self, turns=9):
        self.turns = turns
        self.requests = []
        self.summaries = []

    def complete(self, profile, messages, options, event=None):
        self.last_usage = {}
        if event is None:
            self.summaries.append((copy.deepcopy(profile), copy.deepcopy(messages),
                                   {key: value for key, value in options.items() if key != "cancel_event"}))
            return GOOD_SUMMARY
        self.requests.append(copy.deepcopy(messages))
        return _action(len(self.requests)) if len(self.requests) <= self.turns else "完成"


class _LargeRegistry(_Registry):
    def execute(self, tool, arguments, active, run_context):
        self.executed.append(arguments["n"])
        return True, f"工具{arguments['n']}已完成：" + "已核实的输出资料与操作状态。" * 160


class CurrentTurnCompactionTests(unittest.TestCase):
    def test_first_turn_repeated_compaction_keeps_full_trace_and_replays_view(self):
        for native in (False, True):
            with self.subTest(native=native):
                model = _Model()
                registry = _LargeRegistry(native=native)
                mixin = ConversationRunMixin()
                mixin.app = SimpleNamespace(models=model)
                run = {"conversation_messages": [{"id": "user-1", "role": "user", "content": "完成任务"}]}
                context = {"run_id": "probe"}
                profile = {"kind": "online", "model": "probe", "context_window": 64000}
                events = []
                context["compact_context"] = lambda messages, start, limit, budget, used: mixin._compact_current_trace(
                    run=run, messages=messages, trace_start=start, profile=profile,
                    settings=compaction.resolve_settings({}), limit=limit, budget=budget,
                    used=used, event=events.append, cancel_event=threading.Event(), model_key="probe")
                agent = SkillAgent(_Catalog(), None, model.complete, None)
                answer, runs, _, _ = agent.run("完成任务", [], profile,
                    {"stream": False, "max_tokens": 1024, "max_steps": 20},
                    {"mode": "auto", "skill_ids": []}, [], "", ["echo"], events.append,
                    threading.Event(), tool_registry=registry, run_context=context)
                self.assertEqual(answer, "完成")
                self.assertEqual(registry.executed, list(range(1, 10)))
                self.assertEqual(len(runs), 9)
                self.assertGreaterEqual(len(model.summaries), 2)
                self.assertEqual(len(context["trace_messages"]), 19)
                self.assertEqual(context["trace_messages"][-1]["content"], "完成")
                self.assertEqual(context["trace_view"]["source_user_message_id"], "user-1")
                projected = replay_trace(context["trace_messages"], context["trace_view"])
                self.assertLess(len(str(projected)), len(str(context["trace_messages"])))
                replay = build_model_history([run["conversation_messages"][0], {"role": "assistant", "content": "完成", "metadata": {
                    "trace": context["trace_messages"], "trace_view": context["trace_view"]}}])
                self.assertEqual(sum(m.get("content") == "完成" for m in replay), 1)
                self.assertTrue(any("compacted-summary" in str(m) for m in replay))

    def test_parallel_group_boundary_waits_for_all_results(self):
        messages = [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
                    {"role": "tool", "tool_call_id": "a", "content": "A"}]
        self.assertEqual(complete_tool_boundaries(messages), [])
        messages.append({"role": "tool", "tool_call_id": "b", "content": "B"})
        self.assertEqual(complete_tool_boundaries(messages), [3])

    def test_user_discussing_internal_tags_stays_outside_summary_region(self):
        for tag in ("<compacted-summary>", "<untrusted_tool_result>"):
            with self.subTest(tag=tag):
                model = _Model()
                mixin = ConversationRunMixin()
                mixin.app = SimpleNamespace(models=model)
                trace = []
                for number in range(5):
                    if number == 1:
                        trace.append({"role": "user", "content": f"用户插话（优先处理，并根据新指令继续当前任务）：\n请修复 {tag} 标签"})
                    trace.extend([{"role": "assistant", "tool_calls": [{"id": str(number), "name": "echo", "arguments": {}}]},
                                  {"role": "tool", "tool_call_id": str(number), "content": "资料" * 1500}])
                self.assertFalse(is_summary_message(trace[2]))
                self.assertFalse(is_tool_result_message(trace[2]))
                result = mixin._compact_current_trace(run={}, messages=[{"role": "system"}, *trace], trace_start=1,
                    profile={"context_window": 64000}, settings=compaction.resolve_settings({}),
                    limit=64000, budget=60000, used=20000, event=lambda _: None,
                    cancel_event=threading.Event(), model_key="probe")
                self.assertTrue(result.get("trace"))
                self.assertIn(trace[2], result["trace"])
                self.assertNotIn("用户插话", json.dumps(model.summaries[0][1], ensure_ascii=False))

    def test_fingerprint_mismatch_restores_original_trace(self):
        trace = [{"role": "assistant", "content": "原始执行轨迹"}]
        self.assertIs(replay_trace(trace, {"version": 1, "trace_sha256": "bad", "messages": []}), trace)

    def test_task_completion_hook_does_not_make_another_model_request(self):
        model = _Model(turns=1)
        registry = _Registry()
        context = {"tool_loop_complete": lambda: "文件已经完整确认" if registry.executed else None}
        result = SkillAgent(_Catalog(), None, model.complete, None).run("工作", [],
            {"kind": "online", "model": "probe", "context_window": 64000},
            {"stream": False, "max_tokens": 1024}, {"mode": "auto", "skill_ids": []},
            [], "", ["echo"], lambda _: None, threading.Event(), tool_registry=registry, run_context=context)
        self.assertEqual(result[0], "文件已经完整确认")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len(context["trace_messages"]), 3)

    def test_interjection_reorder_is_saved_once(self):
        pulls = []
        def pull():
            pulls.append(1)
            return [{"id": "interject", "content": "补充要求"}] if len(pulls) == 2 else []
        context = {"pull_interjections": pull}
        model = _Model(turns=0)
        SkillAgent(_Catalog(), None, model.complete, None).run("工作", [],
            {"kind": "online", "model": "probe", "context_window": 64000},
            {"stream": False, "max_tokens": 1024}, {"mode": "auto", "skill_ids": []},
            [], "", [], lambda _: None, threading.Event(), tool_registry=_Registry(), run_context=context)
        trace = context["trace_messages"]
        self.assertEqual(len(trace), 3)
        self.assertEqual(sum("补充要求" in str(m) for m in trace), 1)
        self.assertEqual([m["role"] for m in trace], ["assistant", "user", "assistant"])

    def test_projection_rejects_wrong_run_and_edited_source_user(self):
        source = {"id": "u", "role": "user", "content": "原目标"}
        trace = [{"role": "assistant", "content": "原执行记录"}]
        view = {"version": 1, "trace_sha256": trace_fingerprint(trace), "source_run_id": "run-A",
                "source_user_message_id": "u", "source_user_sha256": source_user_fingerprint(source),
                "messages": [{"role": "assistant", "content": "摘要记录"}]}
        users = {"u": source_user_fingerprint(source)}
        self.assertEqual(replay_trace(trace, view, source_run_id="run-A", source_users=users), view["messages"])
        self.assertIs(replay_trace(trace, view, source_run_id="run-B", source_users=users), trace)
        self.assertIs(replay_trace(trace, view, source_run_id="run-A", source_users={"u": "edited"}), trace)

    def test_window_rejection_retries_only_after_real_reduction_without_output(self):
        from naiba.llm.stream import ContextOverflowError
        for emitted_output in (False, True):
            with self.subTest(emitted_output=emitted_output):
                class RejectOnce(_Model):
                    rejected = False
                    def complete(self, profile, messages, options, event=None):
                        self.requests.append(copy.deepcopy(messages))
                        if len(self.requests) == 1:
                            return _action(1)
                        if len(self.requests) == 2:
                            self.rejected = True
                            if emitted_output:
                                event({"type": "delta", "content": "已经输出的内容"})
                            raise ContextOverflowError("请求输入超出窗口")
                        return "补救完成"
                model = RejectOnce()
                registry = _Registry()
                context = {"compact_context": lambda *_: {"history": [{"role": "user", "content": "简短交接"},
                    {"role": "user", "content": "工作"}]} if model.rejected else {}}
                worker = SkillAgent(_Catalog(), None, model.complete, None)
                def run():
                    return worker.run("工作", [{"role": "user", "content": "较早资料" * 2000},
                        {"role": "assistant", "content": "旧回复"}],
                        {"kind": "online", "model": "probe", "context_window": 64000},
                        {"stream": False, "max_tokens": 1024}, {"mode": "auto", "skill_ids": []},
                        [], "", ["echo"], lambda _: None, threading.Event(), tool_registry=registry, run_context=context)
                if emitted_output:
                    with self.assertRaises(ContextOverflowError):
                        run()
                    self.assertEqual(len(model.requests), 2)
                else:
                    self.assertEqual(run()[0], "补救完成")
                    self.assertEqual(len(model.requests), 3)
                    self.assertLess(len(str(model.requests[2])), len(str(model.requests[1])))
                self.assertEqual(registry.executed, [1], "已完成的工具不因窗口补救重执行")

    def test_parallel_cancellation_keeps_completed_results_and_closed_call_ids(self):
        barrier = threading.Barrier(2)
        cancel = threading.Event()
        registry = _Registry(native=True)
        def execute(tool, arguments, active, context):
            barrier.wait(timeout=5)
            if arguments["n"] == 2:
                cancel.set()
                raise TaskCancelled("任务已取消")
            return True, "第一个工具已完成"
        registry.execute = execute
        model = _Model()
        model.complete = lambda profile, messages, options, event: _action(1, 2)
        context = {}
        with self.assertRaises(TaskCancelled):
            SkillAgent(_Catalog(), None, model.complete, None).run("工作", [],
                {"kind": "online", "model": "probe", "context_window": 64000},
                {"stream": False, "max_tokens": 1024}, {"mode": "auto", "skill_ids": []},
                [], "", ["echo"], lambda _: None, cancel, tool_registry=registry, run_context=context)
        trace = context["trace_messages"]
        self.assertEqual(len(trace), 3)
        ids = [call["id"] for call in trace[0]["tool_calls"]]
        self.assertEqual([message["tool_call_id"] for message in trace[1:]], ids)
        self.assertIn("第一个工具已完成", str(trace))


class SummaryRecoveryTests(unittest.TestCase):
    def test_length_then_success_changes_strategy_and_keeps_normal_profile(self):
        model = _Model()
        calls = []
        profile = {"kind": "local", "request_format": "llama_cpp", "model": "probe",
                   "context_window": 64000, "reasoning_effort": "high"}
        original = copy.deepcopy(profile)
        def complete(selected, messages, options):
            calls.append(copy.deepcopy((selected, messages, options)))
            model.last_finish_reason = "length" if len(calls) == 1 else "stop"
            model.last_usage = {"output_tokens": 8192 if len(calls) == 1 else 300}
            return GOOD_SUMMARY
        model.complete = complete
        result = compaction.request_summary(SimpleNamespace(models=model), [{"role": "user", "content": "材料" * 3000}],
            profile=profile, settings=compaction.resolve_settings({}))
        self.assertEqual(result["summary"], GOOD_SUMMARY.strip())
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0][1], calls[1][1])
        self.assertEqual(profile, original)
        self.assertEqual([a["reason"] for a in result["attempts"]], ["summary_truncated", "accepted"])
        self.assertEqual(result["attempts"][0]["truncation_source"], "finish_reason")

    def test_small_window_and_user_output_limit_are_respected(self):
        model = _Model()
        result = compaction.request_summary(SimpleNamespace(models=model), [{"role": "user", "content": "材料" * 1000}],
            profile={"context_window": 8000, "max_output_tokens": 1000}, settings=compaction.resolve_settings({}))
        self.assertTrue(result["summary"])
        profile, messages, options = model.summaries[0]
        self.assertLessEqual(options["max_tokens"], 1000)
        self.assertLess(compaction.estimate_messages_tokens(messages) + options["max_tokens"], 8000)
        self.assertNotIn("reasoning_tokens", result["attempts"][0])

    def test_cancelled_summary_never_publishes(self):
        cancel = threading.Event()
        model = _Model()
        def complete(*_):
            cancel.set()
            return GOOD_SUMMARY
        model.complete = complete
        with self.assertRaises(TaskCancelled):
            compaction.request_summary(SimpleNamespace(models=model), [{"role": "user", "content": "资料" * 3000}],
                profile={"context_window": 64000}, settings=compaction.resolve_settings({}), cancel_event=cancel)


class TraceCheckpointTests(unittest.TestCase):
    def test_incremental_trace_checkpoint_and_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            conversation = storage.create_conversation()
            run = storage.create_chat_run(conversation["id"], "任务", [], {}, {}, "craft", "")
            trace = [{"role": "assistant", "content": "第一段"}]
            view = {"version": 1, "trace_sha256": trace_fingerprint(trace),
                    "messages": [{"role": "user", "content": "摘要"}]}
            storage.save_context_checkpoint(run["id"], trace, view)
            trace.append({"role": "assistant", "content": "第二段"})
            view = {**view, "trace_sha256": trace_fingerprint(trace)}
            storage.save_context_checkpoint(run["id"], trace, view)
            checkpoint = storage.get_context_checkpoint(run["id"])
            self.assertEqual(checkpoint["trace"], trace)
            self.assertEqual(len(storage.get_run_snapshot(run["id"])["context_checkpoint"]["trace_segments"]), 2)
            message = storage.add_message(conversation["id"], "assistant", "答复", {"trace": trace, "trace_view": view})
            with self.assertRaisesRegex(ValueError, "仍在运行"):
                storage.clear_compaction_marker(message["id"])
            storage.update_background_task(run["id"], status="completed", finished=True)
            self.assertTrue(storage.clear_compaction_marker(message["id"]))
            fresh = storage.get_conversation(conversation["id"])["messages"][-1]
            self.assertNotIn("trace_view", fresh["metadata"])
            self.assertEqual(fresh["metadata"]["trace"], trace)

    def test_cancelled_run_rejects_late_summary_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            conversation = storage.create_conversation()
            run = storage.create_chat_run(conversation["id"], "任务", [], {}, {}, "craft", "")
            storage.update_background_task(run["id"], status="cancelling", cancel_requested=True)
            trace = [{"role": "assistant", "content": "原记录"}]
            view = {"trace_sha256": trace_fingerprint(trace), "version": 1, "messages": []}
            with self.assertRaises(TaskCancelled):
                storage.save_context_checkpoint(run["id"], trace, view, require_active=True)
            self.assertEqual(storage.get_context_checkpoint(run["id"]), {})

    def test_undo_invalidates_later_dependent_descriptors_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            conversation = storage.create_conversation()
            earlier = storage.add_message(conversation["id"], "assistant", "更早", {"compaction": {"summary": "更早摘要"}})
            target = storage.add_message(conversation["id"], "assistant", "目标", {"trace_view": {"summary": "本轮摘要"}, "trace": [{"role": "assistant", "content": "原记录"}]})
            later = storage.add_message(conversation["id"], "assistant", "后续", {"compaction": {"summary": "含本轮摘要的后续摘要"}, "attachments": [{"name": "keep.txt"}]})
            self.assertTrue(storage.clear_compaction_marker(target["id"]))
            messages = {message["id"]: message for message in storage.get_conversation(conversation["id"])["messages"]}
            self.assertIn("compaction", messages[earlier["id"]]["metadata"])
            self.assertNotIn("trace_view", messages[target["id"]]["metadata"])
            self.assertTrue(messages[target["id"]]["metadata"]["trace"])
            self.assertNotIn("compaction", messages[later["id"]]["metadata"])
            self.assertEqual(messages[later["id"]]["metadata"]["attachments"], [{"name": "keep.txt"}])

    def test_branch_remaps_projection_source_and_restart_recovers_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            storage = ChatStorage(path)
            conversation = storage.create_conversation()
            run = storage.create_chat_run(conversation["id"], "任务", [], {}, {}, "craft", "")
            source = storage.get_conversation(conversation["id"])["messages"][0]
            trace = [{"role": "assistant", "content": "完整原始记录"}]
            view = {"trace_sha256": trace_fingerprint(trace), "version": 1,
                    "source_run_id": run["id"], "source_user_message_id": source["id"],
                    "source_user_sha256": source_user_fingerprint(source),
                    "messages": [{"role": "assistant", "content": "检查点摘要"}]}
            storage.save_context_checkpoint(run["id"], trace, view)
            restarted = ChatStorage(path)
            mixin = ConversationRunMixin()
            mixin.app = SimpleNamespace(storage=restarted, record_run_usage=lambda *args, **kwargs: None)
            mixin._flush_sink = lambda _: None
            mixin._all_run_events = lambda _: [{"type": "delta", "content": "中断前已输出"}]
            self.assertEqual(mixin.recover_interrupted_runs(), 1)
            recovered = restarted.get_conversation(conversation["id"])["messages"][-1]
            self.assertEqual(recovered["metadata"]["trace"], trace)
            self.assertEqual(recovered["metadata"]["trace_view"], view)
            next_user = restarted.add_message(conversation["id"], "user", "新问题", {})
            branch = restarted.branch_conversation(conversation["id"], next_user["id"])
            copied = restarted.get_conversation(branch["conversation"]["id"])["messages"]
            self.assertNotEqual(copied[0]["id"], source["id"])
            self.assertEqual(copied[1]["metadata"]["trace_view"]["source_user_message_id"], copied[0]["id"])
            replayed = build_model_history(copied)
            self.assertTrue(any(m.get("content") == "检查点摘要" for m in replayed))
