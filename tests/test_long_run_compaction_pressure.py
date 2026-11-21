"""B2/B3: one input per isolated real Chat Run, with a scripted model transport.

Run with ``python -m tests.test_long_run_compaction_pressure --evidence`` to
emit JSON observations. A green test reproduces today's stopping boundary;
it does not pass B4 (durable goal/continuation).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from naiba import net as net_io
from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.core import compaction
from naiba.core.history import build_model_history
from naiba.core.trace_view import complete_tool_boundaries, is_summary_message
from naiba.paths import PathContext
from naiba.skills.agent import DEFAULT_MAX_STEPS
from tests.test_compaction import GOOD_SUMMARY
from tests.test_llm_runtime_retry import FakeResponse, http_error

MODEL = "gpt-5-pressure-fixture"
PROVIDER_KEY = "online:pressure"
ENDPOINT = "http://127.0.0.1:1/v1/chat/completions"
LEDGER = "progress.txt"
FORBIDDEN = "summary-must-not-write.txt"
FINAL = "资料整理已全部完成。"
PRESSURE_PARTS = 40


def document(number: int, size: int) -> str:
    return f"[part:{number:03}]\n" + "独立的合成资料，用于校验工具结果和摘要隔离。" * size


def tool_call(name: str, arguments: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def reply(content, finish="stop", *, call=None, usage=None):
    message = {"role": "assistant", "content": content}
    if call is not None:
        message["tool_calls"] = [call]
    choice = {"index": 0, "message": message}
    if finish is not None:
        choice["finish_reason"] = finish
    return FakeResponse({"choices": [choice], **({"usage": usage} if usage else {})})


class ScriptedTransport:
    """Use real runtime serialization/parsing/retry, but intercept every open."""

    def __init__(self, workspace: Path, *, parts: int, stop: str, faults: bool):
        self.workspace = workspace
        self.parts = parts
        self.stop = stop
        self.requests = []
        self.chat = []
        self.hidden = []
        self.wire_retry = faults
        self.outcomes = iter(("tool-only", "tool-and-text", "length", "shape", "tool-only") if faults else ())
        self.summary_replies = []

    def open(self, request, *, timeout):
        if request.full_url != ENDPOINT or request.get_method() != "POST":
            raise AssertionError(f"Unexpected model request: {request.full_url}")
        body = json.loads(request.data)
        self.requests.append(body)
        if any(str(m.get("content") or "").startswith(compaction.SUMMARIZE_INSTRUCTION)
               for m in body["messages"]):
            self.hidden.append(body)
            if self.wire_retry:
                self.wire_retry = False
                raise http_error(503, "synthetic transient response")
            outcome = next(self.outcomes, "accepted")
            self.summary_replies.append(outcome)
            usage = {"prompt_tokens": 27001, "completion_tokens": 401, "total_tokens": 27402}
            if outcome.startswith("tool-"):
                return reply("短" * 114 if outcome == "tool-and-text" else None, "tool_calls",
                             call=tool_call("write_file", {"path": FORBIDDEN, "content": "must not execute"},
                                            "hidden-call"), usage=usage if outcome == "tool-only" else None)
            if outcome == "length":
                return reply(GOOD_SUMMARY, "length", usage=usage)
            if outcome == "shape":
                return reply(GOOD_SUMMARY.rstrip() + "\n```python\n未收尾", None)
            return reply(GOOD_SUMMARY, usage=usage)

        self.chat.append(body)
        step = len(self.chat)
        usage = {"prompt_tokens": 90, "completion_tokens": 13, "total_tokens": 103}
        if step == 1:
            name, args = "write_file", {"path": LEDGER, "content": "started\n", "append": True}
        elif step == 5 and self.stop == "reset":
            name, args = "reset_context", {"handoff_path": str(self.workspace / LEDGER)}
        elif step == 5 and self.stop == "provider-error":
            raise http_error(400, "synthetic permanent failure", "Bad Request")
        elif step == 5 and self.stop == "early-text":
            return reply(FINAL, usage=usage)
        elif step <= self.parts + 1:
            name, args = "read_file", {"path": f"part-{step - 1:03}.txt"}
        elif step == self.parts + 2:
            name, args = "write_file", {"path": LEDGER, "content": "verified\n", "append": True}
        else:
            return reply(FINAL, usage=usage)
        return reply(None, "tool_calls", call=tool_call(name, args, f"formal-{step}"), usage=usage)


class LongRunCompactionPressureTests(unittest.TestCase):
    show_evidence = False

    def fixture(self, *, parts=PRESSURE_PARTS, size=175, stop="", faults=False):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="naiba-compaction-pressure-"))
        root = Path(tmp).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        for number in range(1, parts + 1):
            (workspace / f"part-{number:03}.txt").write_text(document(number, size), encoding="utf-8")
        paths = PathContext.local(root, root / "config.json")
        config = ConfigStore(paths.config_path, paths=paths)
        config.data.update({"workspace_dir": str(workspace), "mcp_servers": [],
                            "auto_compaction": {"mode": "auto", "threshold_percent": 30},
                            "providers": [{"id": "pressure", "kind": "online", "name": "Pressure fixture",
                                           "base_url": ENDPOINT.rsplit("/", 2)[0], "api_key": "",
                                           "request_format": "openai_chat", "model": MODEL,
                                           "context_window": 32000, "max_output_tokens": 2048}]})
        config.save()
        fake = ScriptedTransport(workspace, parts=parts, stop=stop, faults=faults)
        self.enterContext(patch.dict(os.environ, {"NAIBA_ERROR_DUMP_DIR": tmp}))
        self.enterContext(patch.object(net_io, "open", side_effect=fake.open))
        app = NaibaChatApp(paths=paths)
        self.addCleanup(app.stop)
        cid = app.storage.create_conversation("隔离压力任务", model_key=PROVIDER_KEY, model_name=MODEL,
            permission_mode="full", workspace_dir=str(workspace), reasoning_effort="high")["id"]
        app.storage.set_enabled_tool_ids(cid, ["read_file", "write_file", "reset_context"])
        app.storage.set_conversation_chat_supports_images(cid, False)
        app.storage.update_conversation_settings(cid, stream_enabled=False)
        self.assertEqual(app.storage.message_count(cid), 0)

        executions = []
        execute = app.tool_registry.execute

        def local_tool(name, args, active, context):
            self.assertIn(name, ("read_file", "write_file", "reset_context"))
            path = (workspace / args.get("path", args.get("handoff_path", ""))).resolve()
            self.assertTrue(path.is_relative_to(workspace) and path != workspace)
            ok, result = execute(name, args, active, context)
            executions.append((name, path.name, ok, result))
            return ok, result

        app.tool_registry.execute = local_tool
        summaries = []
        request_summary = compaction.request_summary

        def observe_summary(*args, **kwargs):
            first_wire = len(fake.hidden)
            result = request_summary(*args, **kwargs)
            summaries.append({"wire": fake.hidden[first_wire:], "result": result})
            return result

        self.enterContext(patch.object(compaction, "request_summary", side_effect=observe_summary))
        view_checks = []
        compact = app.runs._compact_current_trace

        def observe_view(**kwargs):
            messages = kwargs["messages"]
            trace = messages[kwargs["trace_start"]:]
            result = compact(**kwargs)
            next_trace = result.get("trace", trace)
            view_checks.append({"after_formal": len(fake.chat), "summary_attempts": kwargs["run"].get("_summary_calls", 0),
                                "raw_trace_messages": len(messages.trace), "closed_groups": len(complete_tool_boundaries(trace)),
                                "view_tokens_before": compaction.estimate_messages_tokens(trace),
                                "view_tokens_after": compaction.estimate_messages_tokens(next_trace),
                                "used": kwargs["used"], "budget": kwargs["budget"], "limit": kwargs["limit"],
                                "run_status": app.runs.get(kwargs["run"]["id"])["status"]})
            return result

        app.runs._compact_current_trace = observe_view
        prompt = f"按顺序读取隔离工作区中的 {parts} 份资料，全部复核后在进度表登记 verified，不要提前收尾。"
        run_id = app.runs.submit_chat({"conversation_id": cid, "message": prompt})["id"]
        with app.runs._lock:
            thread = app.runs._threads.get(run_id)
        if thread is not None:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "Chat worker did not finish")

        rows = app.storage.get_conversation(cid)["messages"]
        self.assertEqual([row["content"] for row in rows if row["role"] == "user"], [prompt])
        self.assertEqual([row["id"] for row in app.storage.list_conversations()], [cid])
        self.assertEqual([run["id"] for run in app.runs.list()], [run_id], "unexpected follow-up task")
        self.assertIsNone(app.storage.active_run(cid))
        events = app.runs._all_run_events(run_id)
        self.assertFalse(any(e.get("followup_run_id") for e in events))
        assistant, = [row for row in rows if row["role"] == "assistant"]
        trace = assistant["metadata"]["trace"]
        calls = [call for msg in trace for call in msg.get("tool_calls", [])]
        results = [msg for msg in trace if msg["role"] == "tool"]
        expected_files = [LEDGER, *[f"part-{n:03}.txt" for n in range(1, len(executions))]]
        if stop == "reset":
            expected_files[-1] = LEDGER
        self.assertEqual([item[1] for item in executions], expected_files)
        expected_ids = [call["id"] for call in calls]
        self.assertEqual(len(set(expected_ids)), len(executions), "duplicate or missing tool call")
        self.assertEqual([msg["tool_call_id"] for msg in results], expected_ids)
        self.assertEqual(len(complete_tool_boundaries([m for m in trace if m["role"] != "user"])), len(executions))
        for msg, (name, _, ok, result) in zip(results, executions):
            self.assertTrue(ok, name)
            self.assertEqual(json.loads(msg["content"])["result"], result)
        self.assertFalse((workspace / FORBIDDEN).exists())
        self.assertEqual((workspace / LEDGER).read_bytes(), b"started\n", "premature completion or repeated write")
        self.assertFalse(any(row["metadata"].get("compaction") for row in rows), "first-turn tools are not past history")
        self.assertTrue(all(sum(m.get("content") == prompt for m in body["messages"]) == 1 for body in fake.chat))
        return {"fake": fake, "events": events, "assistant": assistant, "rows": rows, "trace": trace,
                "status": app.runs.get(run_id)["status"], "summaries": summaries, "checks": view_checks,
                "checkpoint": app.storage.get_context_checkpoint(run_id), "groups": len(executions),
                "ledger_sha256": hashlib.sha256((workspace / LEDGER).read_bytes()).hexdigest(),
                "usage_totals": app.storage.usage_stats(0)["totals"]}

    def assert_pressure(self, observed):
        fake = observed["fake"]
        summaries = observed["summaries"]
        checks = observed["checks"]
        actions = [e for e in observed["events"] if e["type"] == "compaction"]
        starts = [e for e in actions if e["action"] == "started"]
        results = [e for e in actions if e["action"] in ("compacted", "failed")]
        estimated = [e for e in actions if e["action"] == "estimated"]
        self.assertEqual(len(starts), len(results))
        self.assertEqual(len(results), len(summaries))
        self.assertEqual([e["action"] for e in actions], [a for e in results for a in (
            ["started", "compacted", "estimated"] if e["action"] == "compacted" else ["started", "failed"])])
        attempts = sum(len(item["result"]["attempts"]) for item in summaries)
        self.assertEqual(compaction.MAX_RUN_SUMMARY_ATTEMPTS, 12)
        self.assertEqual(attempts, 12)
        self.assertGreater(observed["groups"], 15)
        after_cap = [c for c in checks if c["summary_attempts"] == attempts]
        self.assertGreater(len(after_cap), 1, "must exercise later eligible groups after the cap")
        self.assertGreaterEqual(after_cap[-1]["closed_groups"], 3)
        self.assertGreaterEqual(after_cap[-1]["used"], after_cap[0]["used"])
        self.assertEqual(sum(len(s["wire"]) for s in summaries), len(fake.hidden))
        for batch in summaries:
            self.assertEqual(sum(a[compaction.NETWORK_SEND_COUNT_KEY] for a in batch["result"]["attempts"]),
                             len(batch["wire"]), "measured runtime dispatches must match intercepted opens")
        self.assertEqual(len(fake.requests), len(fake.hidden) + len(fake.chat))
        for body in fake.requests:
            for key in (compaction.BEFORE_REQUEST_SEND_OPTION, compaction.OBSERVE_NETWORK_SENDS_OPTION,
                        compaction.NETWORK_SEND_COUNT_KEY):
                self.assertNotIn(key, body, "internal observation must not leak into wire payload")
        self.assertTrue(all(not body["stream"] and "tools" not in body and "tool_choice" not in body
                            for body in fake.hidden))
        self.assertTrue(all(body["model"] == MODEL for body in fake.requests))
        self.assertTrue(all(body["reasoning_effort"] == "high" and body["max_tokens"] == 2048
                            and body.get("tools") for body in fake.chat))
        self.assertTrue(all(body.get("reasoning_effort") != "high" and 0 < body["max_tokens"] <= 2048
                            for body in fake.hidden))
        self.assertTrue(all(not is_summary_message(row) for row in observed["trace"]))
        successes = [c for c in checks if c["view_tokens_after"] < c["view_tokens_before"]]
        self.assertEqual(len(successes), len(estimated))
        for check, estimate in zip(successes, estimated):
            following = fake.chat[check["after_formal"]]["messages"]
            self.assertEqual(sum(is_summary_message(row) for row in following), 1)
            self.assertGreater(estimate["context_tokens"], check["view_tokens_after"])
            ids = [m["tool_call_id"] for m in following if m["role"] == "tool"]
            original_ids = [m["tool_call_id"] for m in observed["trace"] if m["role"] == "tool"]
            self.assertEqual(ids, original_ids[check["after_formal"] - 2:check["after_formal"]])
        view = observed["assistant"]["metadata"]["trace_view"]
        self.assertEqual(view["generation"], len(successes))
        self.assertEqual(observed["checkpoint"]["trace_view"]["generation"], view["generation"])
        replay = build_model_history(observed["rows"])
        self.assertEqual(sum(is_summary_message(row) for row in replay), 1)
        self.assertLess(compaction.estimate_messages_tokens(replay), compaction.estimate_messages_tokens(observed["trace"]))
        full, = [e for e in observed["events"] if e["type"] == "context_full"]
        self.assertGreater(full["used"], full["budget"])
        self.assertEqual(observed["groups"], len(fake.chat), "no over-budget request may reach the model")
        self.assertLess(observed["groups"], PRESSURE_PARTS)
        self.assertNotEqual(observed["assistant"]["content"], FINAL)
        # Current loop context_full ends normally at host level; this is not goal success.
        self.assertEqual(observed["status"], "completed")
        usage_events = [e for e in observed["events"] if e["type"] == "usage"]
        self.assertEqual(len(usage_events), len(fake.chat))
        for index, event in enumerate(usage_events, 1):
            self.assertEqual(event["usage"]["requests"], index)
            self.assertEqual(event["usage"]["context_tokens"], 103)
        usage = observed["assistant"]["metadata"]["usage"]
        self.assertEqual((usage["requests"], usage["input_tokens"], usage["context_tokens"]),
                         (len(fake.chat), 90, 103))
        self.assertEqual(observed["usage_totals"]["requests"], len(fake.chat))
        self.assertEqual(observed["usage_totals"]["input_tokens"], 90 * len(fake.chat))
        return results

    def report(self, scenario, observed):
        if not self.show_evidence:
            return
        fake = observed["fake"]
        summaries = observed["summaries"]
        selected = ("type", "sequence", "action", "reason", "context_tokens", "used", "limit", "budget", "error")
        print(json.dumps({"scenario": scenario, "user_messages": 1, "runs": 1, "run_status": observed["status"],
            "goal_verified": False, "b4": "blocked", "formal_requests": len(fake.chat),
            "summary_transport_calls": len(fake.hidden),
            "summary_attempts": sum(len(b["result"]["attempts"]) for b in summaries),
            "summary_network_send_count": sum(a[compaction.NETWORK_SEND_COUNT_KEY]
                for b in summaries for a in b["result"]["attempts"]),
            "summary_batches": [
                {"transport_calls": len(item["wire"]), "attempts": [
                    {key: attempt.get(key) for key in ("attempt", "reason", "finish_reason", "input_tokens",
                        "actual_input_tokens", "output_tokens", "reasoning_tokens", "request_output_tokens",
                        "target_chars", "thinking_effort", "thinking_controlled", "thinking_parameter_type",
                        "truncation_source", compaction.NETWORK_SEND_COUNT_KEY)}
                    for attempt in item["result"]["attempts"]]} for item in summaries],
            "events": [{key: e[key] for key in selected if key in e} for e in observed["events"]
                       if e["type"] in ("compaction", "context_full", "run_failed", "error", "done")],
            "tool_groups": observed["groups"], "raw_trace_messages": len(observed["trace"]),
            "trace_generation": observed["assistant"]["metadata"].get("trace_view", {}).get("generation", 0),
            "view_checks": observed["checks"], "ledger_sha256": observed["ledger_sha256"]},
            ensure_ascii=False), flush=True)

    def test_b2_summary_cap_then_context_guard(self):
        observed = self.fixture()
        results = self.assert_pressure(observed)
        self.assertEqual([e["action"] for e in results], ["compacted"] * 12)
        self.assertEqual(len(observed["fake"].hidden), 12)
        self.report("B2-summary-cap-context-guard", observed)

    def test_b2_default_step_limit(self):
        observed = self.fixture(parts=DEFAULT_MAX_STEPS + 5, size=0)
        self.assertEqual(DEFAULT_MAX_STEPS, 200)
        self.assertEqual(observed["groups"], DEFAULT_MAX_STEPS)
        self.assertEqual(len(observed["fake"].chat), DEFAULT_MAX_STEPS)
        self.assertEqual(observed["status"], "failed")
        failed, = [e for e in observed["events"] if e["type"] == "run_failed"]
        self.assertIn("步数上限", failed["error"])
        self.assertIn("步数上限", observed["assistant"]["metadata"]["error"])
        self.report("B2-default-step-limit", observed)

    def test_b2_reset_early_text_and_error_do_not_continue(self):
        for scenario in ("reset", "early-text", "provider-error"):
            with self.subTest(scenario=scenario):
                observed = self.fixture(stop=scenario)
                self.assertEqual(len(observed["fake"].chat), 5)
                if scenario == "reset":
                    self.assertEqual(observed["status"], "completed")
                    marker = observed["assistant"]["metadata"]["session_start"]
                    self.assertEqual(Path(marker["handoff_path"]).name, LEDGER)
                    done, = [e for e in observed["events"] if e["type"] == "done"]
                    self.assertEqual(done["session_start"], marker)
                    self.assertEqual(build_model_history(observed["rows"]), [])
                elif scenario == "early-text":
                    self.assertEqual(observed["status"], "completed")
                    self.assertEqual(observed["assistant"]["content"], FINAL)
                    self.assertEqual(observed["groups"], 4, "acceptance was not met despite the final text")
                else:
                    self.assertEqual(observed["status"], "failed")
                    self.assertEqual([r["role"] for r in observed["rows"]], ["user", "assistant", "error"])
                    self.assertTrue(observed["assistant"]["metadata"]["partial"])
                    error, = [e for e in observed["events"] if e["type"] == "error"]
                    self.assertIn("synthetic permanent failure", error["message"])
                self.report(f"B2-{scenario}", observed)

    def test_b3_tool_and_length_failures_recover_without_polluting_chat(self):
        observed = self.fixture(faults=True)
        results = self.assert_pressure(observed)
        fake = observed["fake"]
        batches = observed["summaries"]
        failures = [event for event in results if event["action"] == "failed"]
        self.assertEqual([event["reason"] for event in failures], ["summary_tool_call", "summary_truncated"])
        self.assertEqual([e["action"] for e in results[2:]], ["compacted"] * 7)
        first, second, recovery = batches[:3]
        for event, batch, reason in zip(failures, (first, second), ("summary_tool_call", "summary_truncated")):
            self.assertEqual([attempt["reason"] for attempt in event["attempts"]], [reason, reason])
            self.assertEqual([attempt["attempt"] for attempt in event["attempts"]], [1, 2])
            self.assertNotEqual(batch["wire"][-2]["messages"], batch["wire"][-1]["messages"])
            self.assertEqual(batch["wire"][-2]["messages"][:-1], batch["wire"][-1]["messages"][:-1])
        self.assertEqual([a["truncation_source"] for a in failures[1]["attempts"]], ["finish_reason", "shape"])
        self.assertEqual(first["wire"][0], first["wire"][1], "transport retry is not a strategy retry")
        self.assertEqual(fake.summary_replies[:6], ["tool-only", "tool-and-text", "length", "shape", "tool-only", "accepted"])
        self.assertEqual([a["reason"] for a in recovery["result"]["attempts"]], ["summary_tool_call", "accepted"])
        self.assertNotEqual(recovery["wire"][0]["messages"], recovery["wire"][1]["messages"])
        self.assertEqual(len(fake.hidden), 13, "12 summary attempts plus one transport retry")
        self.assertEqual([a[compaction.NETWORK_SEND_COUNT_KEY] for a in first["result"]["attempts"]], [2, 1])
        self.assertEqual([a[compaction.NETWORK_SEND_COUNT_KEY] for a in second["result"]["attempts"]], [1, 1])
        self.assertEqual(failures[0]["attempts"][0]["actual_input_tokens"], 27001)
        self.assertIsNone(failures[0]["attempts"][1].get("actual_input_tokens"), "no stale summary usage")
        original_ids = [row["tool_call_id"] for row in observed["trace"] if row["role"] == "tool"]
        for spent in (2, 4):
            check = next(c for c in observed["checks"] if c["summary_attempts"] == spent)
            self.assertEqual(check["view_tokens_before"], check["view_tokens_after"])
            messages = fake.chat[check["after_formal"]]["messages"]
            self.assertFalse(any(is_summary_message(row) for row in messages))
            self.assertEqual([row["tool_call_id"] for row in messages if row["role"] == "tool"],
                             original_ids[:check["after_formal"]])
        self.assertNotIn("hidden-call", json.dumps(fake.chat))
        self.report("B3-summary-faults", observed)


if __name__ == "__main__":
    if "--evidence" in sys.argv:
        sys.argv.remove("--evidence")
        LongRunCompactionPressureTests.show_evidence = True
    unittest.main()
