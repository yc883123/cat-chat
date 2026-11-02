"""Final-result display and dependency handoff use one fenced reader."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from naiba.core.contracts import (
    SUBAGENT_OUTCOME_KEY, SUPER_AGENT_CLAIMS_KEY, SUPER_AGENT_EXECUTION_KEY,
    SUPER_AGENT_DEPENDENCY_ITEM_CHARS, SUPER_AGENT_DEPENDENCY_MANIFEST_KEY,
)
from naiba.core.messages import dependency_message_content
from naiba.storage.store import ChatStorage
from naiba.super_agent.results import ExecutionResults
from naiba.super_agent.service import SuperAgentService


class ExecutionResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = ChatStorage(Path(self.temp.name) / "chat.db")
        self.service = SuperAgentService(self.storage)
        self.addCleanup(self.service.shutdown)
        self.app = SimpleNamespace(storage=self.storage, config=SimpleNamespace(data={"api_key": "private-api-value"}))
        self.results = ExecutionResults(self.app, self.service)
        self.cid = self.storage.create_conversation()["id"]
        self.thread = self.service.create_thread("local", "test", "result contract")

    def make_node(self, *, kind="subagent", dependencies=(), owner="local", thread=None):
        return self.service.create_node(owner, (thread or self.thread).thread_id, "step",
            idempotency_key=uuid.uuid4().hex,
            executor_type=kind, status="ready" if not dependencies else "blocked",
            dependencies=list(dependencies), checkpoint={"conversation_id": self.cid})

    def finish(self, node, text="UNIQUE_RESULT_913", *, outcome=True, status="completed", partial=False):
        node = self.service.get_node(node.owner_id, node.node_id)
        key, claim, _ = self.service.store.claim_command(node.owner_id, node.thread_id, node.node_id, "start", f"request-{node.attempt}")
        is_chat = node.executor_type == "chat_run"
        snapshot = {SUPER_AGENT_EXECUTION_KEY: claim["correlation"]}
        row = self.storage.create_run(self.cid, "request", {}, snapshot if is_chat else {"params": snapshot},
            kind="chat" if is_chat else "subagent", owner_session_id=self.cid)
        rid = row["id"]
        if is_chat:
            message = self.storage.add_message(self.cid, "assistant", text,
                metadata={"run_id": rid, **({"partial": {"reason": "cut"}} if partial else {})})
            self.storage.update_background_task(rid, status=status, detail={"message_id": message["id"]})
        else:
            result = {"response": text, "trace": "private-trace", "api_key": "private-result-secret"}
            if outcome:
                result[SUBAGENT_OUTCOME_KEY] = {"version": 1, "status": "completed" if status == "completed" else "not_executed"}
            self.storage.update_job(rid, status=status, result=result)
        self.service.store.settle_command(node.owner_id, node.node_id, key, {"executor_id": rid})
        self.service.store.update_node(node.node_id, owner_id=node.owner_id, executor_id=rid, status="running")
        self.service.store.update_node(node.node_id, owner_id=node.owner_id,
            status="succeeded" if status == "completed" else "failed")
        return self.service.get_node(node.owner_id, node.node_id), rid

    def context(self, node):
        return self.results.dependency_context("local", self.thread.thread_id, node.node_id)

    def test_text_handoff_and_view_share_final_body_not_uri_only(self):
        first, _ = self.finish(self.make_node())
        second = self.make_node(dependencies=[first.node_id])
        context = self.context(second)
        shown = self.results.list_results("local", self.thread.thread_id, first.node_id)[0]
        self.assertIn(shown["text"], context["text"])
        self.assertIn("UNIQUE_RESULT_913", dependency_message_content("summarize", context))
        self.assertEqual(context["provided_chars"], len(shown["text"]))
        self.assertFalse(context["truncated"])
        self.assertNotIn("private-trace", json.dumps(shown))
        self.assertNotIn("private-result-secret", json.dumps(shown))
        checkpoint = self.service.get_node("local", second.node_id).checkpoint
        self.assertNotIn("UNIQUE_RESULT_913", json.dumps(checkpoint))
        self.assertIn(SUPER_AGENT_DEPENDENCY_MANIFEST_KEY, checkpoint)

    def test_chat_uses_run_message_not_latest_reply(self):
        first, _ = self.finish(self.make_node(kind="chat_run"), "CORRECT")
        self.storage.add_message(self.cid, "assistant", "WRONG_LATEST", metadata={"run_id": "other"})
        second = self.make_node(dependencies=[first.node_id])
        self.assertIn("CORRECT", self.context(second)["text"])
        self.assertNotIn("WRONG_LATEST", self.context(second)["text"])

    def test_chat_wrong_message_run_and_partial_are_rejected(self):
        first, rid = self.finish(self.make_node(kind="chat_run"), partial=True)
        second = self.make_node(dependencies=[first.node_id])
        with self.assertRaisesRegex(ValueError, "不完整"):
            self.context(second)
        message = self.storage.add_message(self.cid, "assistant", "OTHER", metadata={"run_id": "wrong"})
        self.storage.update_background_task(rid, detail={"message_id": message["id"]})
        with self.assertRaisesRegex(ValueError, "指定 Run"):
            self.context(second)

    def test_other_owner_and_other_thread_rejected(self):
        other = self.service.create_thread("else", "other", "private")
        node = self.make_node(owner="else", thread=other)
        with self.assertRaises(LookupError):
            self.results.list_results("local", other.thread_id, node.node_id)
        another = self.service.create_thread("local", "another", "private")
        node = self.make_node(thread=another)
        with self.assertRaises(LookupError):
            self.results.list_results("local", self.thread.thread_id, node.node_id)

    def test_forged_executor_from_same_conversation_is_rejected(self):
        first, _ = self.finish(self.make_node(), "PRIVATE_FIRST")
        other, rid = self.finish(self.make_node(), "PRIVATE_OTHER")
        self.service.store.update_node(first.node_id, owner_id="local", executor_id=rid)
        second = self.make_node(dependencies=[first.node_id])
        with self.assertRaisesRegex(ValueError, "持久执行关联"):
            self.context(second)
        displayed = self.results.list_results("local", self.thread.thread_id, first.node_id)
        self.assertNotIn("PRIVATE_OTHER", json.dumps(displayed))

    def test_missing_and_legacy_results_do_not_dispatch(self):
        for text, outcome in (("", True), ("legacy body", False)):
            first, _ = self.finish(self.make_node(), text, outcome=outcome)
            second = self.make_node(dependencies=[first.node_id])
            with self.assertRaises(ValueError):
                self.context(second)
            shown = self.results.list_results("local", self.thread.thread_id, first.node_id)[0]
            self.assertIn(shown["compatibility"], {"missing", "legacy_unverified"})

    def test_long_result_view_is_full_but_dependency_budget_stops(self):
        text = "x" * (SUPER_AGENT_DEPENDENCY_ITEM_CHARS + 1)
        first, _ = self.finish(self.make_node(), text)
        second = self.make_node(dependencies=[first.node_id])
        with self.assertRaisesRegex(ValueError, "已提供 0 字符"):
            self.context(second)
        self.assertEqual(self.results.list_results("local", self.thread.thread_id, first.node_id)[0]["text"], text)

    def test_dependency_version_cannot_change_on_consumer_retry(self):
        first, rid = self.finish(self.make_node())
        second = self.make_node(dependencies=[first.node_id])
        context = self.context(second)
        self.assertEqual(self.context(second), context)
        self.storage.update_job(rid, result={"response": "CHANGED", SUBAGENT_OUTCOME_KEY: {"version": 1, "status": "completed"}})
        with self.assertRaisesRegex(ValueError, "结果版本已变化"):
            self.context(second)

    def test_stale_attempt_cannot_supply_current_result(self):
        first, _ = self.finish(self.make_node())
        self.service.store.update_node(first.node_id, owner_id="local", attempt=first.attempt + 1)
        second = self.make_node(dependencies=[first.node_id])
        with self.assertRaisesRegex(ValueError, "attempt"):
            self.context(second)

    def test_unknown_outcome_version_cannot_be_used_as_success(self):
        first, rid = self.finish(self.make_node())
        self.storage.update_job(rid, result={"response": "claim success", SUBAGENT_OUTCOME_KEY: {"version": 2, "status": "completed"}})
        second = self.make_node(dependencies=[first.node_id])
        with self.assertRaisesRegex(ValueError, "执行结论"):
            self.context(second)

    def test_old_attempt_results_remain_viewable(self):
        first, old_id = self.finish(self.make_node(), "OLD_BODY", status="failed")
        self.service.retry_node("local", first.node_id)
        current, new_id = self.finish(first, "NEW_BODY")
        shown = self.results.list_results("local", self.thread.thread_id, current.node_id)
        self.assertEqual([item["executor_id"] for item in shown], [new_id, old_id])
        self.assertEqual([item["text"] for item in shown], ["NEW_BODY", "OLD_BODY"])

    def test_secrets_are_not_returned_from_final_prose(self):
        first, _ = self.finish(self.make_node(), "private-api-value Authorization: Bearer x-secret-123")
        shown = json.dumps(self.results.list_results("local", self.thread.thread_id, first.node_id))
        self.assertNotIn("private-api-value", shown)
        self.assertNotIn("x-secret-123", shown)

    def test_rejected_tool_protocol_body_does_not_expose_unknown_secrets(self):
        prefix = "保留完整正文" * 300
        protocol = prefix + '\n{"arguments":{"command":"curl --token secret-not-in-config"},"password":"another-private"}\n$env:PRIVATE=\'hidden-env-value\'\nhttps://usr:private-pass@example.test/'
        first, _ = self.finish(self.make_node(), protocol, status="failed")
        shown = self.results.list_results("local", self.thread.thread_id, first.node_id)[0]
        self.assertTrue(shown["text"].startswith(prefix))
        for secret in ("secret-not-in-config", "another-private", "hidden-env-value", "private-pass"):
            self.assertNotIn(secret, json.dumps(shown))

    def test_no_dependencies_do_not_read_any_uri_in_user_input(self):
        node = self.make_node()
        self.assertIsNone(self.context(node))


class StartReceiptTests(unittest.TestCase):
    def test_not_submitted_receipt_is_durable_but_new_request_can_start(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = ChatStorage(Path(temp) / "chat.db")
            service = SuperAgentService(storage)
            try:
                thread = service.create_thread("local", "test", "test")
                node = service.create_node("local", thread.thread_id, "test", status="ready", idempotency_key="node")
                args = ("local", thread.thread_id, node.node_id, "start")
                first_key, first, fresh = service.store.claim_command(*args, "first")
                self.assertTrue(fresh)
                service.store.settle_command("local", node.node_id, first_key, {"submission": "not_submitted", "error": "busy"})
                replay_key, replay, fresh = service.store.claim_command(*args, "first")
                self.assertFalse(fresh)
                self.assertEqual(replay_key, first_key)
                self.assertEqual(replay["result"]["error"], "busy")
                next_key, second, fresh = service.store.claim_command(*args, "second")
                self.assertTrue(fresh)
                self.assertNotEqual(next_key, first_key)
                self.assertNotEqual(first["correlation"], second["correlation"])
                self.assertFalse(service.store.claim_command(*args, "third")[2])
                claims = service.get_node("local", node.node_id).checkpoint[SUPER_AGENT_CLAIMS_KEY]
                self.assertEqual(len(claims), 2)
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
