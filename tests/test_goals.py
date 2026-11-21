"""Isolated GOAL continuations use the real chat worker and a fake transport."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from naiba import net as net_io
from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.paths import PathContext
from naiba.storage.goals import GoalConflict
from tests.test_llm_runtime_retry import FakeResponse


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba-goal-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()  # CI 临时目录可能带 8.3 短名，与应用内 resolve 后的长名保持一致
        self.workspace = root / "work"
        self.workspace.mkdir()
        self.paths = PathContext.local(root, root / "config.json")
        config = ConfigStore(self.paths.config_path, paths=self.paths)
        config.data.update({"workspace_dir": str(self.workspace), "mcp_servers": [],
            "auto_compaction": {"mode": "off"},
            "providers": [{"id": "goal-fixture", "kind": "online", "name": "Goal fixture", "api_key": "",
                "base_url": "http://127.0.0.1:1/v1", "request_format": "openai_chat", "model": "goal-fixture",
                "context_window": 32000, "max_output_tokens": 2048}]})
        config.save()
        self.enterContext(patch.dict(os.environ, {"NAIBA_ERROR_DUMP_DIR": str(root / "errors")}))
        self.wire = []
        self.reply = lambda body: self.text("已交付。\nGOAL: completed")
        self.enterContext(patch.object(net_io, "open", side_effect=self.transport))
        self.app = NaibaChatApp(paths=self.paths)
        self.addCleanup(self.app.stop)
        self.cid = self.app.storage.create_conversation("Goal fixture", model_key="online:goal-fixture", model_name="goal-fixture",
            permission_mode="full", workspace_dir=str(self.workspace), reasoning_effort="off")["id"]
        self.app.storage.set_enabled_tool_ids(self.cid, ["read_file", "write_file", "edit_file", "reset_context"])
        self.app.storage.set_conversation_chat_supports_images(self.cid, False)
        self.app.storage.update_conversation_settings(self.cid, stream_enabled=False)

    def transport(self, request, **kwargs):
        self.assertEqual(request.full_url, "http://127.0.0.1:1/v1/chat/completions")
        body = json.loads(request.data)
        self.wire.append(body)
        return FakeResponse(self.reply(body))

    @staticmethod
    def text(content):
        return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}}

    @staticmethod
    def call(name, arguments, call_id):
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]},
            "finish_reason": "tool_calls"}]}

    def submit(self, **extra):
        return self.app.goals.submit({"conversation_id": self.cid, "message": "创建 result.txt。验收后完成。",
                                     "request_id": "one", **extra})

    def wait(self, goal_id, statuses=("awaiting_review", "completed", "blocked", "needs_user", "paused", "cancelled")):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = self.app.goals.store.get(goal_id)
            if row["status"] in statuses:
                return row
            time.sleep(.03)
        self.fail(f"goal timed out: {self.app.goals.latest(self.cid)}")

    def test_create_then_validate_file_and_idempotent_request(self):
        self.reply = lambda body: (self.call("write_file", {"path": "result.txt", "content": "verified"}, "write-one")
            if len(self.wire) == 1 else self.text("已创建。\nGOAL: completed"))
        goal = self.submit(criteria=[{"path": "result.txt", "contains": "verified"}])
        result = self.wait(goal["id"])
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(result["model_requests"], 2)
        self.assertEqual(result["tool_actions"], 1)
        self.assertEqual((self.workspace / "result.txt").read_text(), "verified")
        same = self.submit(criteria=[{"path": "result.txt", "contains": "verified"}])
        self.assertEqual(same["id"], goal["id"])
        rows = self.app.storage.get_conversation(self.cid)["messages"]
        self.assertEqual(sum(row["role"] == "user" for row in rows), 1)
        with self.assertRaises(GoalConflict):
            self.submit(message="different")

    def test_attachment_read_is_scoped_to_current_goal(self):
        attached = self.paths.data_dir / "uploads" / "goal-reference.txt"
        attached.parent.mkdir(parents=True, exist_ok=True)
        attached.write_text("explicit goal attachment", encoding="utf-8")
        other = attached.with_name("unrelated.txt")
        other.write_text("not attached", encoding="utf-8")
        with patch.object(self.app.goals, "start"):
            goal = self.submit(attachments=[{"path": str(attached), "name": attached.name}])
        self.assertTrue(self.app.goals.store.claim(goal["id"], goal["run_id"]))
        snapshot = self.app.storage.get_run_snapshot(goal["run_id"])
        cycle = self.app.goals.cycle(goal["id"], goal["run_id"], snapshot)
        self.assertEqual(cycle.path(attached), attached.resolve())
        with self.assertRaises(GoalConflict):
            cycle.path(attached, writing=True)
        with self.assertRaises(GoalConflict):
            cycle.path(other)

    def test_goal_history_and_cleanup_guards(self):
        goal = self.submit()
        self.wait(goal["id"])
        message_id = self.app.goals.store.get(goal["id"])["input_message_id"]
        operations = [
            lambda: self.app.storage.clear_conversation_messages(self.cid),
            lambda: self.app.storage.truncate_from_message(self.cid, message_id),
            lambda: self.app.storage.delete_message(self.cid, message_id),
            lambda: self.app.storage.set_session_start(self.cid, message_id),
            lambda: self.app.storage.delete_conversation(self.cid),
            lambda: self.app.storage.set_conversation_archived(self.cid, True),
            lambda: self.app.storage.add_run_interjection(self.cid, goal["run_id"], "change goal"),
        ]
        for operation in operations:
            with self.assertRaises(GoalConflict):
                operation()
        self.assertEqual(self.app.storage.delete_conversation_if_idle(self.cid), "busy")
        self.app.storage.clear_terminal_background_tasks()
        self.assertIsNotNone(self.app.runs.get(goal["run_id"]))

    def test_free_goal_delivers_for_review_and_accepts(self):
        goal = self.submit()
        row = self.wait(goal["id"])
        self.assertEqual(row["status"], "awaiting_review", row["reason"])
        final = self.app.goals.control(goal["id"], "accept", row["version"])
        self.assertEqual(final["status"], "completed")

    def test_false_completed_is_corrected_then_stopped(self):
        goal = self.submit(criteria=[{"path": "result.txt"}])
        row = self.wait(goal["id"])
        self.assertEqual(row["status"], "blocked", row["reason"])
        self.assertEqual(row["generation"], 2)
        self.assertEqual(len(self.wire), 2)
        self.assertTrue(any("验收尚未通过" in str(m.get("content")) for m in self.wire[1]["messages"]))

    def test_quantum_continues_without_new_user_or_prefix_reset(self):
        counter = 0
        def produce(body):
            nonlocal counter
            counter += 1
            if counter <= 16:
                return self.call("write_file", {"path": f"result-{counter}.txt", "content": f"part {counter}"}, f"call-{counter}")
            return self.text("完成。\nGOAL: completed")
        self.reply = produce
        goal = self.submit(criteria=[{"path": "result-16.txt"}])
        row = self.wait(goal["id"])
        self.assertEqual(row["status"], "completed", row["reason"])
        self.assertGreaterEqual(row["generation"], 2)
        self.assertEqual(row["tool_actions"], 16)
        for previous, following in zip(self.wire, self.wire[1:]):
            self.assertEqual(following["messages"][:len(previous["messages"])], previous["messages"])
            self.assertEqual(following["tools"], previous["tools"])
        rows = self.app.storage.get_conversation(self.cid)["messages"]
        self.assertEqual(sum(row["role"] == "user" for row in rows), 1)

    def test_goal_blocks_plain_send_until_cancelled(self):
        goal = self.submit()
        row = self.wait(goal["id"])
        with self.assertRaises(GoalConflict):
            self.app.runs.submit_chat({"conversation_id": self.cid, "message": "普通消息"})
        self.app.goals.control(goal["id"], "cancel", row["version"])
        self.wait(goal["id"], ("cancelled",))
        run = self.app.runs.submit_chat({"conversation_id": self.cid, "message": "普通消息"})
        with self.app.runs._lock:
            thread = self.app.runs._threads.get(run["id"])
        if thread:
            thread.join(5)
        self.assertEqual(self.app.runs.get(run["id"])["status"], "completed")
        self.assertNotIn("GOAL 模式", self.wire[-1]["messages"][0]["content"])

    def test_plan_preview_freezes_sources_and_rejects_drift(self):
        plan_file = self.workspace / "plan.md"
        plan_file.write_text("# 阶段一\n\n- [ ] 手工确认\n\n验收文件：result.txt\n", encoding="utf-8")
        plan = self.app.goals.preview_plan({"conversation_id": self.cid, "message": "- [ ] 复读目标",
            "sources": [{"path": str(plan_file), "name": "plan.md"}]})
        self.assertEqual(plan["completion_policy"], "mixed")
        self.assertEqual(len(plan["criteria"]), 1)
        self.assertEqual(plan["criteria"][0]["path"], "result.txt")
        self.assertEqual(len(plan["manual_items"]), 2)
        self.assertEqual(plan["sources"][0]["sha256"],
                         hashlib.sha256(plan_file.read_bytes()).hexdigest())
        self.assertTrue(all("text" not in row for row in plan["sources"]))
        with self.assertRaises(GoalConflict):
            self.app.goals.submit({"conversation_id": self.cid, "message": "正文已改",
                "request_id": "plan-one", "plan": plan})
        plan_file.write_text("# 换了一版\n", encoding="utf-8")
        with self.assertRaises(GoalConflict):
            self.app.goals.submit({"conversation_id": self.cid, "message": "- [ ] 复读目标",
                "request_id": "plan-one", "plan": plan})

    def test_local_check_completes_with_evidence(self):
        self.reply = lambda body: (self.call("write_file", {"path": "data.json",
            "content": json.dumps({"ok": True, "items": ["a"]})}, "write-json")
            if len(self.wire) == 1 else self.text("已创建。\nGOAL: completed"))
        goal = self.submit(criteria=[
            {"path": "data.json"},
            {"kind": "local_check", "check_id": "json_structure", "params": {"path": "data.json",
                "schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}}},
        ])
        row = self.wait(goal["id"])
        self.assertEqual(row["status"], "completed", row["reason"])
        evidence = row["evidence"]
        self.assertEqual(len(evidence), 2)
        self.assertTrue(all(item["passed"] for item in evidence))
        self.assertTrue(any(item.get("check_id") == "json_structure" for item in evidence))

    def test_local_check_failure_corrects_then_blocks(self):
        counter = {"n": 0}
        def produce(body):
            counter["n"] += 1
            if counter["n"] == 1:
                return self.call("write_file", {"path": "data.json", "content": json.dumps({"wrong": 1})}, "write-bad")
            return self.text("已完成。\nGOAL: completed")
        self.reply = produce
        goal = self.submit(criteria=[
            {"path": "data.json"},
            {"kind": "local_check", "check_id": "json_structure", "params": {"path": "data.json",
                "schema": {"type": "object", "required": ["ok"]}}},
        ])
        row = self.wait(goal["id"])
        self.assertEqual(row["status"], "blocked", row["reason"])
        self.assertEqual(row["stop_code"], "check_failed")
        self.assertGreaterEqual(row["generation"], 2)

    def test_extend_is_idempotent_and_only_adds_capacity(self):
        goal = self.submit(criteria=[{"path": "result.txt"}])
        row = self.wait(goal["id"], ("blocked",))
        self.assertEqual(row["status"], "blocked")
        extended = self.app.goals.control(goal["id"], "extend", row["version"],
            extras={"request_id": "add-once", "increments": {"model_requests": 10}, "extra_minutes": 30})
        self.assertEqual(extended["limits"]["model_requests"], 266)
        again = self.app.goals.control(goal["id"], "extend", extended["version"],
            extras={"request_id": "add-once", "increments": {"model_requests": 10}, "extra_minutes": 30})
        self.assertEqual(again["limits"]["model_requests"], 266)
        self.assertEqual(again["status"], "blocked")
        with self.assertRaises(GoalConflict):
            self.app.goals.control(goal["id"], "extend", again["version"],
                extras={"request_id": "add-once", "increments": {"model_requests": 20}, "extra_minutes": 0})
        with self.assertRaises(ValueError):
            self.app.goals.control(goal["id"], "extend", again["version"],
                extras={"request_id": "add-negative", "increments": {"model_requests": -1}, "extra_minutes": 0})
        self.assertEqual(self.app.goals.store.get(goal["id"])["status"], "blocked")


class GoalWirePrefixTests(unittest.TestCase):
    """Compare real serialized requests, not checkpoint/internal message projections."""

    setUp = GoalTests.setUp
    submit = GoalTests.submit
    wait = GoalTests.wait

    def transport(self, request, **kwargs):
        endpoint = {"openai_chat": "chat/completions", "claude": "messages", "codex_responses": "responses"}[self.protocol]
        self.assertEqual(request.full_url, f"http://127.0.0.1:1/v1/{endpoint}")
        body = json.loads(request.data)
        self.wire.append((bytes(request.data), body, (self.app.storage.active_run(self.cid) or {}).get("id")))
        return FakeResponse(self.reply(body))

    def response(self, number=0):
        if not number:
            text = "交付完成。\nGOAL: completed"
            if self.protocol == "openai_chat":
                return GoalTests.text(text)
            if self.protocol == "claude":
                return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
            return {"output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": text}]}], "status": "completed"}
        arguments = {"path": f"prefix-{number}.txt", "content": f"阶段 {number} 已核对"}
        call_id = f"prefix-call-{number}"
        if self.protocol == "openai_chat":
            return GoalTests.call("write_file", arguments, call_id)
        if self.protocol == "claude":
            return {"content": [{"type": "tool_use", "id": call_id, "name": "write_file", "input": arguments}],
                    "stop_reason": "tool_use"}
        return {"output": [{"type": "function_call", "call_id": call_id, "name": "write_file",
                            "arguments": json.dumps(arguments)}], "status": "completed"}

    def fresh_conversation(self):
        cid = self.app.storage.create_conversation("Prefix fixture", model_key="online:goal-fixture",
            model_name="goal-fixture", permission_mode="full", workspace_dir=str(self.workspace),
            reasoning_effort="off")["id"]
        self.app.storage.set_enabled_tool_ids(cid, ["read_file", "write_file", "edit_file", "reset_context"])
        self.app.storage.set_conversation_chat_supports_images(cid, False)
        self.app.storage.update_conversation_settings(cid, stream_enabled=False)
        return cid

    def ordinary(self):
        """Same isolated worker/profile; no GOAL-specific request keys or instruction."""
        cid = self.fresh_conversation()
        self.reply = lambda body: self.response()
        start = len(self.wire)
        run = self.app.runs.submit_chat({"conversation_id": cid, "message": "普通请求"})
        with self.app.runs._lock:
            thread = self.app.runs._threads.get(run["id"])
        if thread:
            thread.join(5)
        self.assertEqual(self.app.runs.get(run["id"])["status"], "completed")
        self.assertEqual(len(self.wire), start + 1)
        return self.wire[-1][1]

    def check_protocol(self, protocol):
        from naiba.run import goals as goal_runtime

        self.protocol = protocol
        self.app.config.data["providers"][0]["request_format"] = protocol
        self.app.config.save()
        plain_before = self.ordinary()
        from itertools import count
        from types import SimpleNamespace
        from naiba.skills import agent as agent_module

        original_boundary = goal_runtime.GoalCycle.boundary
        captures = []
        # Probe each cut independently: compare the first request after that cut
        # byte-for-byte with the same logical request in the uninterrupted run.
        # New tool IDs generated *after* the cut are not previously cached input.
        for cut in (0, 1, 2, 3):
            self.cid = self.fresh_conversation()
            self.wire.clear()
            self.reply = lambda body: self.response(len(self.wire)) if len(self.wire) <= 3 else self.response()
            entropy = count(1)
            uuid_source = SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=f"{next(entropy):08x}" + "0" * 24))

            def boundary(cycle, *args):
                quantum = 2 * cut if cut and cycle.row["generation"] == 1 else 64
                with patch.object(goal_runtime, "_QUANTUM", quantum):
                    return original_boundary(cycle, *args)

            # Patch only agent-local entropy; real GOAL/Run/action IDs stay unique.
            with patch.object(agent_module, "uuid", uuid_source), patch.object(goal_runtime.GoalCycle, "boundary", boundary):
                goal = self.submit(request_id=f"prefix-{cut}",
                    criteria=[{"path": f"prefix-{n}.txt", "contains": f"阶段 {n} 已核对"} for n in range(1, 4)])
                result = self.wait(goal["id"])
            self.assertEqual(result["status"], "completed", result["reason"])
            self.assertEqual((result["model_requests"], result["tool_actions"]), (4, 3))
            self.assertEqual(result["generation"], 2 if cut else 1)
            self.assertEqual(len({run_id for _, _, run_id in self.wire}), 2 if cut else 1)
            self.assertTrue(all(run_id for _, _, run_id in self.wire))
            captures.append(list(self.wire))
            for n in range(1, 4):
                (self.workspace / f"prefix-{n}.txt").unlink()

        sequence_key = "input" if protocol == "codex_responses" else "messages"
        single = captures[0]
        for cut, split in enumerate(captures[1:], 1):
            self.assertNotEqual(split[cut - 1][2], split[cut][2], "expected boundary was not crossed")
            with self.subTest(protocol=protocol, boundary_after_tool=cut):
                for index in range(cut + 1):
                    self.assertEqual(split[index][0], single[index][0],
                                     f"Run cut {cut} changed actual HTTP bytes at logical request {index + 1}")
        for sample in captures:
            for index, (_, body, _) in enumerate(sample):
                with self.subTest(protocol=protocol, request=index + 1, runs=len({row[2] for row in sample})):
                    if index:
                        previous = sample[index - 1][1]
                        self.assertEqual(body[sequence_key][:len(previous[sequence_key])], previous[sequence_key])
                        self.assertEqual(body["tools"], previous["tools"])
                    if protocol == "claude":
                        self.assertEqual(body["system"], single[0][1]["system"])
                        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
                        marked = [(i, j) for i, message in enumerate(body["messages"])
                                  for j, block in enumerate(message["content"]) if "cache_control" in block]
                        self.assertEqual(marked, [(0, 0)], "cache boundary moved to tool_result or a new user")
                        self.assertEqual(body["messages"][0]["content"][0]["cache_control"], {"type": "ephemeral"})
                    elif protocol == "codex_responses":
                        self.assertEqual(body["instructions"], single[0][1]["instructions"])
                        self.assertEqual(sum(item.get("type") == "function_call_output" for item in body["input"]), index)
                    else:
                        self.assertEqual(sum(item["role"] == "tool" for item in body["messages"]), index)

        plain_after = self.ordinary()
        for plain in (plain_before, plain_after):
            self.assertEqual(set(plain), set(single[0][1]), "GOAL leaked a top-level provider field")
            self.assertNotIn("GOAL 模式", json.dumps(plain, ensure_ascii=False))
            self.assertFalse(any("goal" in key.lower() or key in {"request_id", "generation", "run_id", "deadline_at"}
                                 for key in plain))
        self.assertEqual(plain_after, plain_before, "ordinary wire request changed after GOAL execution")

    def test_openai_chat_actual_prefix_single_vs_cross_run(self):
        self.check_protocol("openai_chat")

    def test_claude_actual_prefix_and_cache_breakpoints_single_vs_cross_run(self):
        self.check_protocol("claude")

    def test_responses_actual_prefix_single_vs_cross_run(self):
        self.check_protocol("codex_responses")


if __name__ == "__main__":
    unittest.main()
