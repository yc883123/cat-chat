"""Durable controls, real human Run adoption and observable review costs."""
from __future__ import annotations

import json
import threading
import time
import unittest
from types import MethodType
from unittest.mock import patch

from tests import test_supervised_chat as fixtures
from naiba.super_agent.supervised import SupervisedChatAdapter
from naiba.skills.agent import SkillAgent


class SupervisedRecoveryTests(unittest.TestCase):
    setUp = fixtures.SupervisedAppTests.setUp
    complete = fixtures.SupervisedAppTests.complete
    create = fixtures.SupervisedAppTests.create
    start = fixtures.SupervisedAppTests.start
    wait_phase = fixtures.SupervisedAppTests.wait_phase

    def settled(self):
        with self.adapter.workers_lock:
            threads = list(self.adapter.worker_threads)
        for thread in threads:
            thread.join(4)
            self.assertFalse(thread.is_alive())

    def unknown_review(self):
        original = self.complete
        def complete(profile, messages, options, status=None):
            if messages[0].get("content", "").startswith("你是任务结果审查器"):
                self.calls.append("review")
                value = fixtures.review("needs_user")
                value["criteria_results"][0]["status"] = "unknown"
                return json.dumps(value, ensure_ascii=False)
            return original(profile, messages, options, status)
        self.app.models.complete = complete

    def test_continue_during_review_is_durable_noop_without_fencing_worker(self):
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.create()
        self.start()
        self.wait_phase("reviewing")
        before = self.adapter.store.get(self.tid)
        for request_id in ("continue-click", "continue-click", "second-click"):
            self.adapter.control("local", self.tid, "continue", request_id=request_id)
        after = self.adapter.store.get(self.tid)
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["state"]["generation"], before["state"]["generation"])
        self.gate.set()
        self.wait_phase("completed")
        self.settled()
        self.assertEqual(self.calls, ["execute", "review"])
        # A retry of that click after a later control cannot resume it again.
        self.assertIn("continue-click", self.adapter.store.get(self.tid)["state"]["control_requests"])

    def test_restart_reuses_saved_continue_decision(self):
        self.decisions = ["continue", "done"]
        self.create()
        self.start()
        self.wait_phase("reviewing", "dispatch_ready")
        self.settled()
        self.assertEqual(self.adapter.detail("local", self.tid)["phase"], "dispatch_ready")
        self.adapter = SupervisedChatAdapter(self.app.super_agent_domain.runtime)
        self.assertEqual(self.adapter.detail("local", self.tid)["phase"], "needs_recovery")
        self.adapter.control("local", self.tid, "continue", request_id="restart-continue")
        self.adapter.control("local", self.tid, "continue", request_id="restart-continue")
        self.wait_phase("completed")
        self.settled()
        self.assertEqual(self.calls, ["execute", "review", "execute", "review"])

    def test_saved_needs_user_is_not_rechecked_by_repeated_continue(self):
        self.decisions = ["needs_user"]
        self.create()
        self.start()
        self.wait_phase("waiting_user")
        self.settled()
        self.adapter = SupervisedChatAdapter(self.app.super_agent_domain.runtime)
        for i in range(3):
            self.adapter.control("local", self.tid, "continue", request_id=f"click-{i}")
            self.adapter.sync("local", self.tid, self.nid)
        self.assertEqual(self.calls, ["execute", "review"])
        self.assertEqual(self.adapter.detail("local", self.tid)["phase"], "waiting_user")

    def test_human_followup_uses_new_run_and_preserves_previous_result(self):
        self.decisions = ["needs_user", "done"]
        self.create()
        self.start()
        old = self.wait_phase("waiting_user")["run_id"]
        self.settled()
        human = self.app.runs.submit_chat({"conversation_id": self.cid, "message": "补充：请使用明确结尾完成文案"})
        self.assertEqual(self.adapter.detail("local", self.tid)["phase"], "paused")
        self.adapter.control("local", self.tid, "continue", request_id="accept-human-baseline")
        state = self.wait_phase("completed")
        self.settled()
        self.assertEqual(state["run_id"], human["id"])
        self.assertNotEqual(state["run_id"], old)
        self.assertEqual(state["round"], 1)
        self.assertEqual(self.calls, ["execute", "review", "execute", "review"])
        current = self.adapter.store.rounds(self.tid)[-1]
        self.assertEqual(current["prior_runs"][0]["run_id"], old)
        self.assertTrue(current["adopted_human"])
        self.assertEqual(current["correlation"], "")
        self.assertEqual(self.app.super_agent.get_node("local", self.nid).executor_id, human["id"])

    def test_user_acceptance_after_pause_is_explicit_and_idempotent(self):
        self.unknown_review()
        self.create()
        self.start()
        self.wait_phase("waiting_user")
        self.settled()
        self.adapter.control("local", self.tid, "pause", request_id="pause")
        self.assertTrue(self.adapter.detail("local", self.tid)["can_accept_completion"])
        for _ in range(2):
            self.adapter.control("local", self.tid, "accept_completion", request_id="user-accept")
        state = self.adapter.detail("local", self.tid)
        self.assertEqual(state["completion_source"], "user")
        self.assertEqual(state["last_review"]["criteria_results"][0]["status"], "unknown")
        self.assertEqual(self.app.super_agent.get_thread("local", self.tid).status, "completed")
        self.assertEqual(self.app.super_agent.get_node("local", self.nid).status, "succeeded")
        self.assertEqual(self.adapter.store.get(self.tid)["active"], 0)

    def test_failed_or_stale_review_cannot_be_user_accepted(self):
        self.decisions = ["needs_user"]
        self.create()
        self.start()
        self.wait_phase("waiting_user")
        self.settled()
        self.assertFalse(self.adapter.detail("local", self.tid)["can_accept_completion"])
        with self.assertRaisesRegex(Exception, "无法判断"):
            self.adapter.control("local", self.tid, "accept_completion", request_id="bad-accept")
        self.adapter.human_input(self.cid)
        self.assertFalse(self.adapter.detail("local", self.tid)["can_accept_completion"])

    def test_review_usage_and_internal_retries_are_visible(self):
        original = self.complete
        def complete(runtime, profile, messages, options, status=None):
            checking = messages[0].get("content", "").startswith("你是任务结果审查器")
            result = original(profile, messages, options, status)
            runtime.last_usage = {"input_tokens": 11 if checking else 20, "output_tokens": 7 if checking else 10, "total_tokens": 18 if checking else 30}
            runtime.last_diagnostics = {"attempts": 2 if checking else 1}
            return result
        self.app.models.complete = MethodType(complete, self.app.models)
        self.create()
        self.start()
        self.wait_phase("completed")
        self.settled()
        state = self.adapter.detail("local", self.tid)
        self.assertIsNone(state["token_usage"]["total_tokens"])
        self.assertEqual(state["token_usage"]["known_tokens"], 48)
        self.assertTrue(state["token_usage"]["has_unknown"])
        self.assertEqual(state["review_attempt_count"], 2)
        self.assertEqual(state["review_retry_count"], 1)
        self.assertEqual(state["execution_attempt_count"], 1)
        self.assertEqual(state["execution_retry_count"], 0)

    def test_shutdown_joins_actual_worker_before_database_cleanup(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        original = self.complete
        def held(profile, messages, options, status=None):
            if messages[0].get("content", "").startswith("你是任务结果审查器"):
                entered.set()
                release.wait(4)
            return original(profile, messages, options, status)
        self.app.models.complete = held
        self.addCleanup(release.set)
        self.create()
        self.start()
        self.wait_phase("reviewing")
        self.assertTrue(entered.wait(2))
        def stop():
            self.adapter.shutdown(4)
            stopped.set()
        stopper = threading.Thread(target=stop)
        stopper.start()
        try:
            self.assertFalse(stopped.wait(.05))
            release.set()
            self.assertTrue(stopped.wait(3))
            stopper.join(1)
            self.assertFalse(self.adapter.worker_threads)
        finally:
            release.set()
            stopper.join(4)

    def test_background_dependency_waits_for_real_terminal_state(self):
        self.create()
        self.start()
        record = self.adapter.store.get(self.tid)
        with self.app.runs._lock:
            worker = self.app.runs._threads.get(record["state"]["run_id"])
        if worker:
            worker.join(4)
        job = self.app.storage.create_run(self.cid, "后台检查", {}, {}, kind="shell", parent_job_id=record["state"]["run_id"], owner_session_id=self.cid)
        state = self.wait_phase("waiting_background")
        self.assertEqual(self.calls, ["execute"])
        self.app.storage.update_job(job["id"], status="completed", result={"text": "验收记录完成"}, finished=True)
        self.wait_phase("completed")
        self.settled()
        self.assertEqual(self.calls, ["execute", "review"])

    def test_partial_reply_and_truncated_events_never_complete(self):
        self.create()
        self.start()
        rid = self.adapter.store.get(self.tid)["state"]["run_id"]
        with self.app.runs._lock:
            worker = self.app.runs._threads.get(rid)
        if worker:
            worker.join(4)
        run = self.app.storage.get_background_task(rid)
        self.app.storage.merge_message_metadata(self.cid, run["detail"]["message_id"], {"partial": True})
        self.wait_phase("waiting_user")
        self.settled()
        self.assertNotEqual(self.app.super_agent.get_thread("local", self.tid).status, "completed")

    def test_changed_agent_content_prevents_new_dispatch_without_mutating_conversation(self):
        self.create()
        node = self.app.super_agent.get_node("local", self.nid)
        before = self.app.storage.get_conversation(self.cid, include_messages=False)
        original = self.app.config.get_agent
        def changed(agent_id):
            agent = dict(original(agent_id) or {})
            agent["system_prompt"] = str(agent.get("system_prompt") or "") + "变更"
            return agent
        with patch.object(self.app.config, "get_agent", side_effect=changed):
            self.start()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.adapter.detail("local", self.tid)["phase"], "waiting_user")
        after = self.app.storage.get_conversation(self.cid, include_messages=False)
        self.assertEqual(before["enabled_tool_ids"], after["enabled_tool_ids"])

    def test_baseline_change_in_submission_transaction_creates_no_run_and_can_retry(self):
        self.create()
        original = self.app.storage.create_chat_run
        def changed(*args, **kwargs):
            self.app.storage.update_conversation_settings(self.cid, permission_mode="auto")
            return original(*args, **kwargs)
        with patch.object(self.app.storage, "create_chat_run", side_effect=changed):
            self.start()
        state = self.adapter.detail("local", self.tid)
        self.assertEqual(state["round"], 0)
        self.assertEqual(state["rounds"][-1]["phase"], "not_submitted")
        self.assertEqual(self.app.storage.get_conversation(self.cid)["messages"], [])
        self.assertIsNone(self.app.storage.active_run(self.cid))
        self.app.storage.update_conversation_settings(self.cid, permission_mode="confirm")
        self.adapter.control("local", self.tid, "continue", request_id="retry-zero-submission")
        self.wait_phase("completed")
        self.settled()
        self.assertEqual(self.calls, ["execute", "review"])

    def test_baseline_change_after_registration_blocks_the_model_request(self):
        self.create()
        original = SkillAgent.run
        def changed(worker, *args, **kwargs):
            self.app.storage.update_conversation_settings(self.cid, permission_mode="auto")
            return original(worker, *args, **kwargs)
        with patch.object(SkillAgent, "run", autospec=True, side_effect=changed):
            self.start()
            rid = self.adapter.store.get(self.tid)["state"]["run_id"]
            with self.app.runs._lock:
                worker = self.app.runs._threads.get(rid)
            if worker:
                worker.join(4)
        self.assertEqual(self.app.storage.get_background_task(rid)["status"], "failed")
        self.assertEqual(self.calls, [])

    def test_completion_saved_before_event_recovers_interrupted_node(self):
        self.create()
        self.start()
        original = self.adapter.service.emit_event
        def fail_completion(*args, **kwargs):
            if len(args) > 2 and args[2] == "run_completed":
                raise OSError("crash after durable decision")
            return original(*args, **kwargs)
        with patch.object(self.adapter.service, "emit_event", side_effect=fail_completion):
            self.wait_phase("reviewing", "completed")
            self.settled()
        self.assertEqual(self.adapter.detail("local", self.tid)["phase"], "completed")
        self.app.super_agent.recover_all()
        self.assertEqual(self.app.super_agent.get_node("local", self.nid).status, "interrupted")
        self.adapter = SupervisedChatAdapter(self.app.super_agent_domain.runtime)
        self.adapter.sync("local", self.tid, self.nid)
        self.assertEqual(self.app.super_agent.get_thread("local", self.tid).status, "completed")
        self.assertEqual(self.app.super_agent.get_node("local", self.nid).status, "succeeded")
        self.assertEqual(self.calls, ["execute", "review"])

    def test_run_registered_before_binding_reconciles_without_resubmitting(self):
        self.create()
        with patch.object(self.adapter, "_bind"):
            self.start()
        original_run = self.adapter.store.get(self.tid)["state"]["run_id"]
        self.adapter = SupervisedChatAdapter(self.app.super_agent_domain.runtime)
        self.adapter.control("local", self.tid, "continue", request_id="recover-registered")
        self.wait_phase("completed")
        self.settled()
        self.assertEqual(self.app.super_agent.get_node("local", self.nid).executor_id, original_run)
        self.assertEqual(self.calls, ["execute", "review"])

    def test_cancel_waiting_for_review_acknowledgement_survives_deadline(self):
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.create()
        self.start()
        state = self.wait_phase("reviewing")
        self.adapter.control("local", self.tid, "cancel", request_id="cancel-check")
        with patch("naiba.super_agent.supervised._now", return_value=state["deadline_at"] + 1):
            self.adapter.sync("local", self.tid, self.nid)
            self.assertEqual(self.adapter.detail("local", self.tid)["phase"], "stopping")
            self.gate.set()
            self.settled()
            self.adapter.sync("local", self.tid, self.nid)
        self.assertEqual(self.adapter.detail("local", self.tid)["phase"], "cancelled")
        self.assertEqual(self.app.super_agent.get_thread("local", self.tid).status, "cancelled")
        self.assertEqual(self.adapter.store.get(self.tid)["active"], 0)

    def test_no_progress_stops_before_another_side_effect_round(self):
        self.decisions = ["continue", "continue"]
        self.create()
        self.start()
        state = self.wait_phase("blocked")
        self.settled()
        self.assertEqual(state["round"], 2)
        self.assertEqual(self.calls, ["execute", "review", "execute", "review"])
        self.assertIn("新进展", state["error"])

    def test_reworded_assistant_claim_is_not_objective_progress(self):
        self.decisions = ["continue", "continue"]
        sequence = 0
        def complete(profile, messages, options, status=None):
            nonlocal sequence
            if messages[0].get("content", "").startswith("你是任务结果审查器"):
                self.calls.append("review")
                return json.dumps(fixtures.review(self.decisions.pop(0), objective=True), ensure_ascii=False)
            self.calls.append("execute")
            sequence += 1
            return f"第{sequence}轮：已完成所有真实测试，无需任何验证记录。"
        self.app.models.complete = complete
        self.create(supervision={"completion_criteria": ["相关测试通过"], "max_continuations": 3, "window_minutes": 30})
        self.start()
        state = self.wait_phase("blocked")
        self.settled()
        self.assertEqual(state["round"], 2)
        self.assertEqual(self.calls, ["execute", "review", "execute", "review"])

    def test_event_read_truncation_does_not_treat_missing_evidence_as_success(self):
        self.create()
        self.start()
        rid = self.adapter.store.get(self.tid)["state"]["run_id"]
        with self.app.runs._lock:
            worker = self.app.runs._threads.get(rid)
        if worker:
            worker.join(4)
        for i in range(201):
            self.app.storage.append_run_event(rid, {"type": "status", "message": f"检查记录{i}"})
        record = self.adapter.store.get(self.tid)
        run = self.app.storage.get_background_task(rid)
        payload, _, incomplete = self.adapter._evidence(record, run)
        self.assertTrue(incomplete)
        self.assertGreater(payload["omitted"]["events_total"], payload["omitted"]["events_provided"])
        self.wait_phase("waiting_user")
        self.settled()
        self.assertEqual(self.calls, ["execute", "review"])


if __name__ == "__main__":
    unittest.main()
