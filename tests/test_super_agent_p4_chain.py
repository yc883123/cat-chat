from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from naiba.app import _super_agent_event_payload, _super_agent_node_payload
from naiba.super_agent.domain import build_novel_graph, build_writer_graph
from naiba.super_agent.store import SuperAgentStore

from tests.test_super_agent_p3_writer_novel import P3App


class SuperAgentP4ChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_p4_chain_")
        self.app = P3App(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def _writer_book(self) -> str:
        return self.app.writer.store.create_book(title="P4 Writer")['id']

    def _chain_body(self, book_id: str, *, key: str = "chain-create-1") -> dict:
        return {
            "domain": "writer",
            "book_id": book_id,
            "chain": "writer_v1",
            "chain_version": 1,
            "chain_instance_id": "writer-chain-stable",
            "title": "Writer P4",
            "goal": "完成阶段链",
            "model_name": "probe",
            "model_key": "probe",
            "provider_id": "probe",
            "idempotency_key": key,
            "stage_requests": {"setup": {}, "outline": {}, "chapter_plan": {}, "chapter_content": {}},
        }

    def test_graph_builders_are_ordered_linear_contracts(self) -> None:
        for builder, expected in ((build_writer_graph, ("setup", "outline", "chapter_plan", "chapter_content")),
                                  (build_novel_graph, ("import", "elements", "outline", "screenplay", "review"))):
            graph = builder()
            self.assertEqual([item["stage_key"] for item in graph], list(expected))
            self.assertEqual(graph[0]["dependencies"], [])
            for index, item in enumerate(graph[1:], start=1):
                self.assertEqual(item["dependencies"], [expected[index - 1]])
                self.assertEqual(item["promote_to"], expected[index + 1] if index + 1 < len(expected) else "")

    def test_model_selection_requires_existing_global_profile_and_rejects_credentials(self) -> None:
        book_id = self._writer_book()
        with self.assertRaisesRegex(ValueError, "供应商或模型不存在"):
            self.app.super_agent_domain.create_thread("local", {
                "domain": "writer", "book_id": book_id, "stage": "chapter",
                "start_request": {
                    "kind": "chapter", "chapter_no": 1,
                    "provider_id": "missing", "model_key": "online:missing", "model_name": "probe",
                },
            })
        with self.assertRaisesRegex(ValueError, "不接受 API Key"):
            self.app.super_agent_domain.create_thread("local", {
                "domain": "writer", "book_id": book_id, "stage": "chapter",
                "start_request": {"kind": "chapter", "chapter_no": 1, "model_name": "probe", "api_key": "secret"},
            })
        self.assertEqual(self.app.super_agent.list_threads("local"), [])

    def test_chain_create_is_multi_node_ready_blocked_and_idempotent(self) -> None:
        book_id = self._writer_book()
        first_thread, first_node = self.app.super_agent_domain.create_thread("local", self._chain_body(book_id))
        nodes = self.app.super_agent.list_nodes("local", first_thread.thread_id)
        self.assertEqual(len(nodes), 4)
        self.assertEqual(nodes[0].status, "ready")
        self.assertTrue(all(node.status == "blocked" for node in nodes[1:]))
        self.assertEqual(first_thread.current_node_id, nodes[0].node_id)
        self.assertEqual(first_node.node_id, nodes[0].node_id)
        self.assertEqual(nodes[0].checkpoint["start_request"]["model_name"], "probe")
        self.assertEqual(nodes[0].checkpoint["start_request"]["model_key"], "probe")
        self.assertEqual(nodes[0].checkpoint["start_request"]["provider_id"], "probe")
        self.assertEqual([node.sequence for node in nodes], [0, 1, 2, 3])
        self.assertEqual(nodes[0].promote_to, nodes[1].node_id)
        self.assertEqual(nodes[1].dependencies, [nodes[0].node_id])
        replay_thread, replay_node = self.app.super_agent_domain.create_thread("local", self._chain_body(book_id))
        self.assertEqual(replay_thread.thread_id, first_thread.thread_id)
        self.assertEqual(replay_node.node_id, first_node.node_id)
        self.assertEqual(len(self.app.super_agent.list_nodes("local", first_thread.thread_id)), 4)

    def test_invalid_graph_rolls_back_thread_and_nodes(self) -> None:
        store = SuperAgentStore(self.app.storage)
        with self.assertRaises(ValueError):
            store.create_thread_chain(
                owner_id="local", title="bad", goal="bad", chain_instance_id="bad-chain", chain_version=1,
                nodes=[
                    {"stage_key": "a", "sequence": 0, "status": "ready", "dependencies": [], "promote_to": "missing", "kind": "a"},
                    {"stage_key": "b", "sequence": 2, "status": "blocked", "dependencies": ["a"], "kind": "b"},
                ],
            )
        self.assertEqual(store.list_threads(owner_id="local"), [])

    def test_success_promotes_once_and_emits_public_stage_event(self) -> None:
        thread, _ = self.app.super_agent_domain.create_thread("local", self._chain_body(self._writer_book()))
        nodes = self.app.super_agent.list_nodes("local", thread.thread_id)
        first = nodes[0]
        self.app.super_agent.emit_event(
            "local", thread.thread_id, "run_started", node_id=first.node_id,
            source="test", source_event_id="p4-start-1", idempotency_key="p4-start-1", attempt=0,
        )
        self.app.super_agent.emit_event(
            "local", thread.thread_id, "run_completed", node_id=first.node_id,
            source="test", source_event_id="p4-success-1", idempotency_key="p4-success-1", attempt=0,
        )
        current = self.app.super_agent.get_thread("local", thread.thread_id)
        after = self.app.super_agent.list_nodes("local", thread.thread_id)
        self.assertEqual(current.current_node_id, after[1].node_id)
        self.assertEqual(after[1].status, "ready")
        self.assertEqual(sum(1 for event in self.app.super_agent.events("local", thread.thread_id) if event.event_type == "stage_promoted"), 1)
        # A replay with a distinct event key is auditable but cannot promote a
        # completed source again or append another stage event.
        self.app.super_agent.emit_event(
            "local", thread.thread_id, "run_completed", node_id=first.node_id,
            source="test", source_event_id="p4-success-duplicate", idempotency_key="p4-success-duplicate", attempt=0,
        )
        self.assertEqual(self.app.super_agent.get_thread("local", thread.thread_id).current_node_id, after[1].node_id)
        self.assertEqual(sum(1 for event in self.app.super_agent.events("local", thread.thread_id) if event.event_type == "stage_promoted"), 1)
        promoted = [event for event in self.app.super_agent.events("local", thread.thread_id) if event.event_type == "stage_promoted"][0]
        public = _super_agent_event_payload(promoted)
        self.assertEqual(public["summary"]["from_stage"], "setup")
        self.assertEqual(public["summary"]["to_stage"], "outline")
        self.assertNotIn("checkpoint", public)

    def test_replayed_or_damaged_graph_is_rejected_without_repair(self) -> None:
        book_id = self._writer_book()
        body = self._chain_body(book_id, key="chain-replay-check")
        thread, _ = self.app.super_agent_domain.create_thread("local", body)
        store = self.app.super_agent.store
        db = store._connect()
        try:
            db.execute("DELETE FROM super_nodes WHERE thread_id=? AND stage_key=?", (thread.thread_id, "outline"))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(RuntimeError):
            self.app.super_agent_domain.create_thread("local", body)
        self.assertEqual(len(self.app.super_agent.list_nodes("local", thread.thread_id)), 3)

    def test_final_success_completes_thread_and_emits_terminal_promotion(self) -> None:
        thread, _ = self.app.super_agent_domain.create_thread("local", self._chain_body(self._writer_book(), key="chain-final"))
        nodes = self.app.super_agent.list_nodes("local", thread.thread_id)
        for node in nodes[:-1]:
            self.app.super_agent.emit_event(
                "local", thread.thread_id, "run_started", node_id=node.node_id,
                source="test", source_event_id=f"final-start-{node.sequence}", idempotency_key=f"final-start-{node.sequence}", attempt=0,
            )
            self.app.super_agent.emit_event(
                "local", thread.thread_id, "run_completed", node_id=node.node_id,
                source="test", source_event_id=f"final-{node.sequence}", idempotency_key=f"final-{node.sequence}", attempt=0,
            )
        final = self.app.super_agent.get_node("local", nodes[-1].node_id)
        self.app.super_agent.emit_event(
            "local", thread.thread_id, "run_started", node_id=final.node_id,
            source="test", source_event_id="final-last-start", idempotency_key="final-last-start", attempt=0,
        )
        self.app.super_agent.emit_event(
            "local", thread.thread_id, "run_completed", node_id=final.node_id,
            source="test", source_event_id="final-last", idempotency_key="final-last", attempt=0,
        )
        self.assertEqual(self.app.super_agent.get_thread("local", thread.thread_id).status, "completed")
        terminal = [event for event in self.app.super_agent.events("local", thread.thread_id) if event.event_type == "stage_promoted" and event.payload.get("terminal")]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0].payload["from_stage"], "chapter_content")
        self.assertEqual(terminal[0].payload["to_node_id"], "")

    def test_novel_chain_allows_per_stage_models_and_keeps_gate_stages_non_domain(self) -> None:
        staged = self.app.novel.store.stage_import(filename="per-stage.txt", data="第1章 标题\n原文。".encode(), max_len=6000)
        book = self.app.novel.store.create_book(import_id=staged["import_id"], picked=[1], title="Per Stage Novel")
        body = {
            "domain": "novel", "book_id": book["id"], "chain": "novel_v1",
            "chain_instance_id": "novel-per-stage", "provider_id": "probe", "model_key": "probe", "model_name": "default-model", "stage_requests": {
                "elements": {"model_name": "elements-model", "chapter_ids": [1]},
                "outline": {"model_name": "outline-model", "chapter_ids": [1]},
                "screenplay": {"model_name": "script-model", "chapter_ids": [1]},
            },
        }
        thread, _ = self.app.super_agent_domain.create_thread("local", body)
        nodes = self.app.super_agent.list_nodes("local", thread.thread_id)
        self.assertEqual([node.stage_key for node in nodes], ["import", "elements", "outline", "screenplay", "review"])
        self.assertEqual(nodes[0].checkpoint["domain_ref"]["stage"], "import")
        self.assertTrue(nodes[0].checkpoint["domain_ref"]["coordination"])
        self.assertEqual(nodes[0].checkpoint["start_request"].get("model_name", ""), "")
        self.assertEqual(nodes[1].checkpoint["start_request"]["model_name"], "elements-model")
        self.assertEqual(nodes[2].checkpoint["start_request"]["model_name"], "outline-model")
        self.assertEqual(nodes[3].checkpoint["start_request"]["stage"], "script")
        self.assertEqual(nodes[4].checkpoint["domain_ref"]["stage"], "review")
        self.assertNotIn("model_name", nodes[0].checkpoint["start_request"])
        self.assertNotIn("model_name", nodes[4].checkpoint["start_request"])

    def test_failed_or_old_attempt_does_not_promote(self) -> None:
        thread, _ = self.app.super_agent_domain.create_thread("local", self._chain_body(self._writer_book(), key="chain-fail"))
        first = self.app.super_agent.list_nodes("local", thread.thread_id)[0]
        self.app.super_agent.emit_event(
            "local", thread.thread_id, "run_started", node_id=first.node_id,
            source="test", source_event_id="p4-fail-start", idempotency_key="p4-fail-start", attempt=0,
        )
        self.app.super_agent.emit_event(
            "local", thread.thread_id, "job_failed", node_id=first.node_id,
            source="test", source_event_id="p4-fail", idempotency_key="p4-fail", attempt=0,
        )
        self.assertEqual(self.app.super_agent.get_thread("local", thread.thread_id).current_node_id, first.node_id)
        self.assertEqual(self.app.super_agent.list_nodes("local", thread.thread_id)[1].status, "blocked")

        # A retry creates attempt 1; a late attempt-0 success remains fenced.
        self.app.super_agent.retry_node("local", first.node_id)
        self.app.super_agent.emit_event(
            "local", thread.thread_id, "run_completed", node_id=first.node_id,
            source="test", source_event_id="p4-old-success", idempotency_key="p4-old-success", attempt=0,
        )
        self.assertEqual(self.app.super_agent.get_thread("local", thread.thread_id).current_node_id, first.node_id)
        self.assertEqual(self.app.super_agent.list_nodes("local", thread.thread_id)[1].status, "blocked")

    def test_novel_approval_apply_promotes_next_stage(self) -> None:
        staged = self.app.novel.store.stage_import(filename="p4.txt", data="第1章 标题\n原文。".encode(), max_len=6000)
        book = self.app.novel.store.create_book(import_id=staged["import_id"], picked=[1], title="P4 Novel")
        body = {
            "domain": "novel", "book_id": book["id"], "chain": "novel_v1",
            "chain_instance_id": "novel-chain-approval", "model_name": "probe",
            "stage_requests": {stage: {"chapter_ids": [1]} for stage in ("import", "elements", "outline", "screenplay", "review")},
        }
        thread, first = self.app.super_agent_domain.create_thread("local", body)
        started = self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "start", "node_id": first.node_id, "request_id": "novel-chain-start",
        })
        # ``import`` is a coordination gate: it validates the already-created
        # book and completes without submitting a Studio task.  The next node
        # is the first model-backed Novel stage.
        self.assertEqual(started.node_status, "succeeded")
        nodes_after_import = self.app.super_agent.list_nodes("local", thread.thread_id)
        elements = nodes_after_import[1]
        started = self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "start", "node_id": elements.node_id, "request_id": "novel-elements-start",
        })
        self.app.studio.jobs.tasks[started.executor_id].update(status="done", result="要素结果")
        self.app.super_agent_domain.action("local", thread.thread_id, {
            "action": "sync", "node_id": elements.node_id, "attempt": 0,
        })
        waiting = self.app.super_agent.get_node("local", elements.node_id)
        self.assertEqual(waiting.status, "waiting_user")
        approval_id = waiting.checkpoint["approval_id"]
        self.app.super_agent_domain.resolve_approval("local", thread.thread_id, approval_id, {
            "status": "approved", "request_id": "novel-chain-approve",
        })
        nodes = self.app.super_agent.list_nodes("local", thread.thread_id)
        self.assertEqual(nodes[0].status, "succeeded")
        self.assertEqual(nodes[1].status, "succeeded")
        self.assertEqual(nodes[2].status, "ready")
        self.assertEqual(self.app.super_agent.get_thread("local", thread.thread_id).current_node_id, nodes[2].node_id)

if __name__ == "__main__":
    unittest.main()
