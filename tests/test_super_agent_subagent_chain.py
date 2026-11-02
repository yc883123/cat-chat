"""Real JobRegistry/SkillAgent/ToolExecutor; only model completion is local data."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.core.contracts import SUBAGENT_OUTCOME_KEY, SUPER_AGENT_CLAIMS_KEY
from naiba.core.exceptions import ActiveRunError
from naiba.jobs import JobSpec
from naiba.paths import PathContext
from naiba.run.manager import ConversationRunManager
from naiba.super_agent.baseline import PreflightRejected


class SubagentChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        paths = PathContext.local(self.root, self.root / "config.json")
        config = ConfigStore(paths.config_path, paths=paths)
        config.data.update({"providers": [{"id": "probe", "kind": "online", "name": "offline",
            "base_url": "http://127.0.0.1:1/v1", "api_key": "", "request_format": "openai_chat", "model": "probe"}],
            "workspace_dir": str(self.root / "workspace"), "mcp_servers": []})
        config.save()
        self.app = NaibaChatApp(paths=paths)
        self.addCleanup(self.shutdown)
        self.runtime = self.app.super_agent_domain.runtime
        self.runtime.shutdown()
        self.cid = self.app.storage.create_conversation(title="子助手隔离验收", provider_id="probe",
            model_key="online:probe", model_name="probe", permission_mode="confirm",
            workspace_dir=str(self.root / "workspace"))["id"]
        self.answers = []
        self.calls = []
        self.profiles = []
        self.app.models.complete = self.complete
        self.app.models.last_finish_reason = "stop"

    def shutdown(self):
        for job_id, worker in list(self.app.jobs._threads.items()):
            self.app.jobs.cancel(job_id)
            worker.join(5)
            self.assertFalse(worker.is_alive(), "Job worker must release SQLite before temporary cleanup")
        self.app.stop()

    def complete(self, profile, messages, options, status=None):
        self.calls.append(messages)
        self.profiles.append(profile)
        return self.answers.pop(0) if self.answers else "唯一正文 SUBAGENT-RESULT-731 已完成"

    def create(self, domain="subagent", **request):
        field = "instruction" if domain == "subagent" else "message"
        body = {"domain": domain, "conversation_id": self.cid, "goal": "隔离执行",
            "start_request": {field: "执行本地测试", **request}}
        return self.app.super_agent_domain.create_thread("local", body)

    def command(self, thread, node, action="start", request="start"):
        return self.runtime.command("local", thread.thread_id, node.node_id, action, request)

    def wait(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        self.fail("local execution did not reach expected state")

    def finished(self, thread, node):
        current = self.app.super_agent.get_node("local", node.node_id)
        record = self.wait(lambda: (row if (row := self.app.jobs.get(current.executor_id))["status"] in {"failed", "completed", "cancelled"} else None))
        self.app.jobs._threads[current.executor_id].join(5)
        self.runtime.domain.registry.get("subagent").sync("local", thread.thread_id, node.node_id)
        return record, self.app.super_agent.get_node("local", node.node_id)

    def tool(self, *, fenced=False):
        action = json.dumps({"type": "tool", "tool": "list_directory", "arguments": {"path": str(self.root)}})
        self.answers = ["```json\n" + action + "\n```" if fenced else action, "完成 SUBAGENT-RESULT-731"]

    def pending(self, result):
        return self.wait(lambda: self.app.runs.subagent_confirmation_snapshot(result.executor_id))[0]

    def test_plain_text_is_completed_and_default_does_not_fork(self):
        self.app.storage.add_message(self.cid, "user", "PRIVATE-PARENT-HISTORY")
        thread, node = self.create()
        result = self.command(thread, node)
        record, current = self.finished(thread, node)
        self.assertEqual((record["status"], current.status), ("completed", "succeeded"))
        self.assertEqual(record["result"][SUBAGENT_OUTCOME_KEY]["status"], "completed")
        self.assertNotIn("PRIVATE-PARENT-HISTORY", json.dumps(self.calls))
        self.assertFalse(self.app.runs.subagent_confirmation_snapshot(result.executor_id))

    def test_native_and_fenced_approval_execute_once(self):
        for fenced in (False, True):
            self.tool(fenced=fenced)
            thread, node = self.create(allowed_tools=["list_directory"])
            result = self.command(thread, node)
            confirmation = self.pending(result)
            self.runtime.domain.registry.get("subagent").sync("local", thread.thread_id, node.node_id)
            self.assertEqual(self.app.super_agent.get_node("local", node.node_id).status, "waiting_user")
            self.assertEqual(confirmation["action_source"], "fenced" if fenced else "native")
            self.assertTrue(self.app.runs.confirm_tool_async(result.executor_id, confirmation["confirm_id"])[0])
            self.app.runs.confirm_tool_async(result.executor_id, confirmation["confirm_id"])
            record, current = self.finished(thread, node)
            self.assertEqual((record["status"], current.status), ("completed", "succeeded"))
            self.assertEqual(len(record["result"]["tool_runs"]), 1)

    def test_rejection_is_not_executed_and_no_model_retry(self):
        self.tool()
        thread, node = self.create(allowed_tools=["list_directory"])
        result = self.command(thread, node)
        confirmation = self.pending(result)
        self.app.runs.reject_tool(result.executor_id, confirmation["confirm_id"])
        record, current = self.finished(thread, node)
        self.assertEqual((record["status"], current.status), ("failed", "failed"))
        self.assertEqual(record["result"][SUBAGENT_OUTCOME_KEY]["status"], "not_executed")
        self.assertEqual(len(self.calls), 1)

    def test_running_permission_change_expires_confirmation(self):
        self.tool()
        thread, node = self.create(allowed_tools=["list_directory"])
        result = self.command(thread, node)
        confirmation = self.pending(result)
        self.app.storage.update_conversation_settings(self.cid, permission_mode="full")
        record, current = self.finished(thread, node)
        self.assertEqual((record["status"], current.status), ("failed", "failed"))
        self.assertEqual(record["result"][SUBAGENT_OUTCOME_KEY]["status"], "not_executed")
        self.assertFalse(self.app.runs.confirm_tool_async(result.executor_id, confirmation["confirm_id"]))
        self.assertEqual(len(self.calls), 1)

    def test_cancel_waiting_wins_and_unregisters(self):
        self.tool()
        thread, node = self.create(allowed_tools=["list_directory"])
        result = self.command(thread, node)
        self.pending(result)
        self.command(thread, node, "cancel", "cancel")
        record, current = self.finished(thread, node)
        self.assertEqual((record["status"], current.status), ("cancelled", "cancelled"))
        self.assertFalse(self.app.runs.subagent_confirmation_snapshot(result.executor_id))

    def test_plain_child_without_ui_fails_promptly(self):
        self.tool()
        job_id = self.app.jobs.start(JobSpec(kind="subagent", conversation_id=self.cid,
            owner_session_id=self.cid, params={"instruction": "读取目录", "fork": False, "allowed_tools": ["list_directory"]}), owner=self.cid)
        record = self.app.jobs.wait(job_id, 5, owner=self.cid)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["result"][SUBAGENT_OUTCOME_KEY]["reason_code"], "confirmation_unavailable")
        self.assertEqual(len(self.calls), 1)

    def test_preflight_refusal_persisted_and_replay_stays_refused(self):
        thread, node = self.create()
        self.app.storage.update_conversation_settings(self.cid, permission_mode="full")
        with self.assertRaises(PreflightRejected):
            self.command(thread, node)
        current = self.app.super_agent.get_node("local", node.node_id)
        self.assertEqual(current.status, "ready")
        self.assertEqual(next(iter(current.checkpoint[SUPER_AGENT_CLAIMS_KEY].values()))["result"]["submission"], "not_submitted")
        self.assertFalse(self.calls)
        self.app.storage.update_conversation_settings(self.cid, permission_mode="confirm")
        with self.assertRaises(PreflightRejected):
            self.command(thread, node)
        self.command(thread, node, request="new-user-start")
        self.assertEqual(self.finished(thread, node)[0]["status"], "completed")

    def test_claim_boundary_change_settles_no_job(self):
        thread, node = self.create()
        original = self.app.super_agent.store.claim_command
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.app.storage.update_conversation_settings(self.cid, permission_mode="full")
            return result
        with patch.object(self.app.super_agent.store, "claim_command", side_effect=changed), self.assertRaises(PreflightRejected):
            self.command(thread, node)
        current = self.app.super_agent.get_node("local", node.node_id)
        self.assertFalse(current.executor_id)
        self.assertEqual(next(iter(current.checkpoint[SUPER_AGENT_CLAIMS_KEY].values()))["result"]["submission"], "not_submitted")
        self.assertFalse(self.calls)

    def test_registration_transaction_rejects_last_moment_permission_change(self):
        thread, node = self.create()
        original = self.app.storage.create_run
        def changed(*args, **kwargs):
            self.app.storage.update_conversation_settings(self.cid, permission_mode="full")
            return original(*args, **kwargs)
        with patch.object(self.app.storage, "create_run", side_effect=changed), self.assertRaises(PreflightRejected):
            self.command(thread, node)
        self.assertFalse(self.app.jobs.list(owner=self.cid))
        self.assertFalse(self.calls)
        current = self.app.super_agent.get_node("local", node.node_id)
        self.assertEqual(next(iter(current.checkpoint[SUPER_AGENT_CLAIMS_KEY].values()))["result"]["submission"], "not_submitted")

    def test_busy_chat_is_not_unknown_and_can_cancel(self):
        thread, node = self.create(domain="chat")
        with patch.object(self.app.runs, "submit_chat", side_effect=ActiveRunError("existing")):
            with self.assertRaises(PreflightRejected):
                self.command(thread, node)
        self.command(thread, node, "cancel", "cancel")
        self.assertEqual(self.app.super_agent.get_node("local", node.node_id).status, "cancelled")

    def test_resume_rejected_before_claim(self):
        thread, node = self.create()
        self.assertFalse(self.runtime.node_capabilities(node)["resume"])
        with self.assertRaisesRegex(RuntimeError, "断点续跑"):
            self.command(thread, node, "resume", "bad-resume")
        current = self.app.super_agent.get_node("local", node.node_id)
        self.assertFalse(current.checkpoint.get(SUPER_AGENT_CLAIMS_KEY))
        self.assertFalse(self.calls)

    def test_confirmation_parameter_redaction(self):
        parameters = ConversationRunManager._confirmation_parameters({"command":
            'curl --token "secret with spaces" -H "Authorization: Bearer abcdef"; $env:OTHER="hidden value"',
            "path": "safe", "api_key": "top-secret"})
        encoded = json.dumps(parameters)
        for secret in ("secret with spaces", "abcdef", "hidden value", "top-secret"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(parameters["path"], "safe")

    def test_dependency_body_reaches_second_subagent_without_job_output(self):
        thread, first = self.app.super_agent_domain.create_thread("local", {"domain": "mixed", "goal": "两步正文",
            "nodes": [{"domain": "subagent", "conversation_id": self.cid, "start_request": {"instruction": "生产唯一标记"}},
                      {"domain": "subagent", "conversation_id": self.cid, "start_request": {"instruction": "总结前序结果"}}]})
        self.command(thread, first)
        self.finished(thread, first)
        nodes = self.app.super_agent.list_nodes("local", thread.thread_id)
        second = next(n for n in nodes if n.node_id != first.node_id)
        self.assertEqual(second.status, "ready")
        self.command(thread, second, request="second")
        record, current = self.finished(thread, second)
        self.assertEqual(current.status, "succeeded")
        self.assertIn("SUBAGENT-RESULT-731", json.dumps(self.calls[-1]))
        snapshot = self.app.storage.get_run_snapshot(current.executor_id)
        self.assertNotIn("job_output", snapshot["params"]["allowed_tools"])
        self.assertNotIn("subagent", snapshot["params"]["allowed_tools"])
        self.assertFalse(snapshot["params"]["fork"])

    def test_ordinary_tool_failure_can_recover_to_success(self):
        self.answers = [json.dumps({"type": "tool", "tool": "read_file", "arguments": {"path": "missing.txt"}}),
                        "文件不存在；已用现有资料完成分析。"]
        thread, node = self.create(allowed_tools=["read_file"])
        self.command(thread, node)
        record, current = self.finished(thread, node)
        self.assertEqual(current.status, "succeeded")
        self.assertFalse(record["result"]["tool_runs"][0]["success"])

    def test_retry_rechecks_baseline_before_new_attempt(self):
        self.tool()
        thread, node = self.create(allowed_tools=["list_directory"])
        result = self.command(thread, node)
        pending = self.pending(result)
        self.app.runs.reject_tool(result.executor_id, pending["confirm_id"])
        self.finished(thread, node)
        self.app.storage.update_conversation_settings(self.cid, workspace_dir=str(self.root / "changed"))
        with self.assertRaises(PreflightRejected):
            self.command(thread, node, "retry", "retry")
        current = self.app.super_agent.get_node("local", node.node_id)
        self.assertEqual(current.attempt, 0)
        self.assertEqual(current.executor_id, result.executor_id)
        self.assertEqual(len(self.calls), 1)

    def test_legacy_completed_without_outcome_does_not_promote(self):
        thread, node = self.create()
        result = self.command(thread, node)
        self.app.jobs.wait(result.executor_id, 5)
        self.app.jobs._threads[result.executor_id].join(5)
        self.app.storage.update_job(result.executor_id, result={"response": "旧记录声称完成"})
        _, current = self.finished(thread, node)
        self.assertEqual(current.status, "failed")

    def test_late_approval_after_attempt_change_cannot_execute(self):
        self.tool()
        thread, node = self.create(allowed_tools=["list_directory"])
        result = self.command(thread, node)
        pending = self.pending(result)
        self.app.super_agent.store.update_node(node.node_id, owner_id="local", status="interrupted")
        self.app.super_agent.retry_node("local", node.node_id)
        self.assertFalse(self.app.runs.confirm_tool_async(result.executor_id, pending["confirm_id"]))
        record = self.app.jobs.wait(result.executor_id, 5)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["result"][SUBAGENT_OUTCOME_KEY]["status"], "not_executed")

    def test_task_model_override_second_step_and_failed_retry_preserve_conversation(self):
        thread, first = self.app.super_agent_domain.create_thread("local", {"domain": "mixed", "goal": "任务级模型",
            "nodes": [{"domain": "chat", "conversation_id": self.cid, "start_request": {"message": "第一步", "model_name": "task-model"}},
                      {"domain": "chat", "conversation_id": self.cid, "start_request": {"message": "第二步", "model_name": "task-model"}}]})
        def settle_chat(node):
            current = self.app.super_agent.get_node("local", node.node_id)
            row = self.wait(lambda: (value if (value := self.app.storage.get_background_task(current.executor_id))["status"] in {"completed", "failed"} else None))
            self.wait(lambda: current.executor_id not in self.app.runs._threads)
            self.runtime.domain.registry.get("chat_run").sync("local", thread.thread_id, node.node_id)
            return row
        self.command(thread, first)
        self.assertEqual(settle_chat(first)["status"], "completed")
        second = next(n for n in self.app.super_agent.list_nodes("local", thread.thread_id) if n.node_id != first.node_id)
        self.assertEqual(second.status, "ready")
        with patch.object(self.app.models, "complete", side_effect=RuntimeError("offline deliberate failure")):
            self.command(thread, second, request="second")
            self.assertEqual(settle_chat(second)["status"], "failed")
        self.command(thread, second, action="retry", request="second-retry")
        self.assertEqual(settle_chat(second)["status"], "completed")
        conversation = self.app.storage.get_conversation(self.cid)
        self.assertEqual(conversation["model_name"], "probe")
        self.assertTrue(self.profiles)
        self.assertTrue(all(profile["model"] == "task-model" for profile in self.profiles))

    def test_unknown_outcome_version_cannot_complete(self):
        thread, node = self.create()
        result = self.command(thread, node)
        self.app.jobs.wait(result.executor_id, 5)
        self.app.jobs._threads[result.executor_id].join(5)
        self.app.storage.update_job(result.executor_id, result={"response": "未来结果", SUBAGENT_OUTCOME_KEY: {"version": 999, "status": "completed"}})
        _, current = self.finished(thread, node)
        self.assertEqual(current.status, "failed")

    def test_unknown_legacy_executor_nodes_api_remains_read_only(self):
        thread = self.app.super_agent.create_thread("local", "旧执行器记录")
        node = self.app.super_agent.create_node("local", thread.thread_id, "legacy",
            executor_type="uninstalled_executor", idempotency_key="legacy", status="ready")
        payload, status = self.app.api_super_agent_nodes(thread.thread_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["nodes"][0]["capabilities"], {"start": False, "cancel": False, "retry": False, "resume": False})
        with self.assertRaisesRegex(RuntimeError, "未知 executor_type"):
            self.command(thread, node)
        self.assertEqual(self.app.super_agent.get_node("local", node.node_id).status, "ready")
        self.assertFalse(self.app.jobs.list(owner=self.cid))
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
