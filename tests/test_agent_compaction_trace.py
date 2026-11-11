"""Loop compaction must preserve each current-turn tool record exactly once."""
from __future__ import annotations

import copy
import json
import threading
import unittest

from naiba.core.exceptions import TaskCancelled
from naiba.core.history import build_model_history
from naiba.core.messages import MetadataKeys
from naiba.skills.agent import SkillAgent


class _Catalog:
    def scan(self):
        return []


class _Registry:
    def __init__(self, native=False, reset_on=None):
        self.native = native
        self.reset_on = reset_on
        self.executed = []

    def schemas(self):
        return [{
            "name": "echo", "description": "Return the supplied number",
            "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}},
        }] if self.native else []

    def side_effect(self, name):
        return False

    def media_declaration(self, name):
        return {"extract": "none", "policy": "never"}

    def execute(self, tool, arguments, active, run_context):
        number = arguments["n"]
        self.executed.append(number)
        if number == self.reset_on:
            run_context["context_reset"] = {"handoff_path": "handoff.md"}
        return True, f"result-{number}"


class _Model:
    last_usage = {}
    last_reasoning_id = ""
    last_finish_reason = "stop"

    def __init__(self, responses, cancel=None):
        self.responses = responses
        self.cancel = cancel
        self.requests = []
        self.last_reasoning = ""

    def complete(self, profile, messages, options, event):
        self.requests.append(copy.deepcopy(messages))
        index = len(self.requests) - 1
        self.last_reasoning = f"reasoning-{index + 1}"
        if self.cancel is not None and index == 1:
            self.cancel.set()
        return self.responses[index]


class _Compactor:
    def __init__(self, *, noop=False, budget=None):
        self.calls = 0
        self.noop = noop
        self.budget = budget

    def __call__(self, messages, trace_start, limit, budget, used):
        self.calls += 1
        if self.noop:
            return {}
        return {
            "history": [{"role": "user", "content": f"history-summary-{self.calls}"}],
            "limit": limit, "budget": budget if self.budget is None else self.budget,
        }


def _action(*numbers):
    calls = [{"type": "tool", "tool": "echo", "arguments": {"n": n}} for n in numbers]
    return json.dumps(calls[0] if len(calls) == 1 else {"type": "tools", "calls": calls})


class AgentCompactionTraceTests(unittest.TestCase):
    def _run_agent(self, model, registry, context, *, steps=20, cancel=None, history=None):
        events = []
        agent = SkillAgent(_Catalog(), None, model.complete, None)
        result = agent.run(
            "current question", history or [],
            {"kind": "online", "model": "probe", "context_window": 128000},
            {"stream": False, "max_tokens": 1024, "max_steps": steps},
            {"mode": "auto", "skill_ids": []}, [], "", ["echo"], events.append,
            cancel, tool_registry=registry, run_context=context,
        )
        return result, events

    def _assert_native_pairs(self, messages, numbers):
        calls = [call for item in messages for call in item.get("tool_calls", [])]
        results = [item for item in messages if item.get("role") == "tool"]
        identifiers = [call["id"] for call in calls]
        self.assertEqual([call["arguments"]["n"] for call in calls], numbers)
        self.assertEqual(len(set(identifiers)), len(numbers), "each call ID must appear only once")
        self.assertEqual([item["tool_call_id"] for item in results], identifiers)
        self.assertEqual([json.loads(item["content"])["result"] for item in results],
                         [f"result-{n}" for n in numbers])
        pending = []
        for item in messages:
            if item.get("tool_calls"):
                self.assertFalse(pending, "finish previous results before another assistant call")
                pending = [call["id"] for call in item["tool_calls"]]
            if item.get("role") == "tool":
                self.assertTrue(pending)
                self.assertEqual(item["tool_call_id"], pending.pop(0))
        self.assertFalse(pending)

    def _assert_compat_calls(self, messages, numbers):
        calls = []
        results = []
        for item in messages:
            content = str(item.get("content") or "")
            if item.get("role") == "assistant" and content.startswith('{"type": "tool"'):
                calls.append(json.loads(content)["arguments"]["n"])
            if "<untrusted_tool_result>" in content:
                results.append(content)
        self.assertEqual(calls, numbers)
        self.assertEqual(len(results), len(numbers))
        for number, result in zip(numbers, results):
            self.assertIn(f"result-{number}", result)

    def test_single_and_repeated_compaction_persist_compat_trace_once(self):
        for count in (1, 3):
            with self.subTest(compactions=count):
                compact = _Compactor()
                context = {"compact_context": compact}
                model = _Model([*[_action(n) for n in range(1, count + 1)], "done"])
                (answer, runs, _, _), _ = self._run_agent(model, _Registry(), context)
                trace = context["trace_messages"]
                self.assertEqual(compact.calls, count)
                self.assertEqual(len(runs), count)
                self.assertEqual(len(trace), count * 2 + 1)
                self._assert_compat_calls(trace, list(range(1, count + 1)))
                self.assertEqual(trace[-1]["content"], answer)
                self.assertFalse(any("history-summary" in str(item) for item in trace))

    def test_native_pairs_survive_repeated_compaction_and_next_turn_replay(self):
        compact = _Compactor()
        context = {"compact_context": compact}
        model = _Model([_action(1, 2), _action(3, 4), "done"])
        (answer, _, _, _), _ = self._run_agent(model, _Registry(native=True), context)
        trace = context["trace_messages"]
        self.assertEqual(compact.calls, 2)
        self.assertEqual(len(trace), 7)
        self._assert_native_pairs(trace, [1, 2, 3, 4])
        self.assertEqual([item["reasoning_content"] for item in trace if item["role"] == "assistant"],
                         ["reasoning-1", "reasoning-2", "reasoning-3"])
        history = build_model_history([
            {"role": "user", "content": "current question"},
            {"role": "assistant", "content": answer, "metadata": {MetadataKeys.TRACE: trace}},
        ])
        self.assertEqual(history[1:], trace, "saved trace must replay unchanged, including the final answer")
        next_model = _Model(["next answer"])
        self._run_agent(next_model, _Registry(native=True), {}, history=history)
        self._assert_native_pairs(next_model.requests[0], [1, 2, 3, 4])
        self.assertEqual(next_model.requests[0][2:-1], trace)

    def test_noop_compaction_does_not_drop_or_duplicate_trace(self):
        compact = _Compactor(noop=True)
        context = {"compact_context": compact}
        model = _Model([_action(1), _action(2), "done"])
        self._run_agent(model, _Registry(), context)
        self.assertEqual(compact.calls, 2)
        self.assertEqual(len(context["trace_messages"]), 5)
        self._assert_compat_calls(context["trace_messages"], [1, 2])

    def test_cancellation_after_compaction_keeps_completed_pairs_once(self):
        cancel = threading.Event()
        compact = _Compactor()
        context = {"compact_context": compact}
        model = _Model([_action(1, 2), "discarded after cancellation"], cancel=cancel)
        with self.assertRaises(TaskCancelled):
            self._run_agent(model, _Registry(native=True), context, cancel=cancel)
        self.assertEqual(compact.calls, 1)
        self.assertEqual(len(context["trace_messages"]), 3)
        self._assert_native_pairs(context["trace_messages"], [1, 2])

    def test_step_limit_after_repeated_compaction_keeps_results_once(self):
        compact = _Compactor()
        context = {"compact_context": compact}
        model = _Model([_action(1), _action(2), _action(3)])
        (answer, runs, _, _), events = self._run_agent(model, _Registry(native=True), context, steps=3)
        self.assertIn("步数上限", answer)
        self.assertEqual(len(runs), 3)
        self.assertEqual(compact.calls, 2)
        self.assertTrue(any(item["type"] == "run_failed" for item in events))
        self._assert_native_pairs(context["trace_messages"], [1, 2, 3])
        reminders = [item for item in context["trace_messages"] if "步数将尽" in str(item.get("content"))]
        self.assertEqual(len(reminders), 1)

    def test_context_full_after_compaction_keeps_completed_pairs_once(self):
        compact = _Compactor(budget=1)
        context = {"compact_context": compact}
        model = _Model([_action(1, 2)])
        (_, runs, _, _), events = self._run_agent(model, _Registry(native=True), context)
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(model.requests), 1)
        self.assertTrue(any(item["type"] == "context_full" for item in events))
        self.assertEqual(len(context["trace_messages"]), 3)
        self._assert_native_pairs(context["trace_messages"], [1, 2])

    def test_reset_after_compaction_keeps_completed_trace_and_final_answer_once(self):
        context = {"compact_context": _Compactor()}
        model = _Model([_action(1), _action(2)])
        (answer, _, _, _), _ = self._run_agent(model, _Registry(native=True, reset_on=2), context)
        self.assertIn("已交接", answer)
        self.assertEqual(len(context["trace_messages"]), 5)
        self._assert_native_pairs(context["trace_messages"], [1, 2])
        self.assertEqual(context["trace_messages"][-1]["content"], answer)

    def test_unexecuted_fence_after_compaction_keeps_prior_results_once(self):
        context = {"compact_context": _Compactor(), "confirmation_ui": False}
        model = _Model([_action(1), "```json\n" + _action(2) + "\n```"])
        registry = _Registry(native=True)
        (answer, _, _, _), _ = self._run_agent(model, registry, context)
        self.assertIn("未执行", answer)
        self.assertEqual(registry.executed, [1])
        self.assertEqual(len(context["trace_messages"]), 3)
        self._assert_native_pairs(context["trace_messages"], [1])
        self.assertEqual(context["trace_messages"][-1]["content"], answer)

    def test_parse_failure_after_compaction_keeps_prior_results_once(self):
        malformed = '<tool name="echo"><parameter name="n">2'
        context = {"compact_context": _Compactor()}
        model = _Model([_action(1), malformed, malformed, malformed])
        (answer, _, _, _), _ = self._run_agent(model, _Registry(native=True), context)
        self.assertEqual(context["partial"]["attempts"], 3)
        self.assertEqual(len(context["trace_messages"]), 7)
        self._assert_native_pairs(context["trace_messages"], [1])
        self.assertEqual(context["trace_messages"][-1]["content"], answer)


if __name__ == "__main__":
    unittest.main()
