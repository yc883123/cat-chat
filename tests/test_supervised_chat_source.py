"""Trusted internal message origin and atomic human takeover boundaries."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from naiba.config import ConfigStore
from naiba.core.history import build_model_history
from naiba.core.messages import MetadataKeys
from naiba.run.manager import ConversationRunManager
from naiba.storage.store import ChatStorage
from naiba.super_agent.service import SuperAgentService


class TrustedChatSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.storage = ChatStorage(root / "chat.db")
        self.service = SuperAgentService(self.storage)
        self.addCleanup(self.service.shutdown)
        self.conversation = self.storage.create_conversation(model_name="fixture")
        self.cid = self.conversation["id"]
        self.thread = self.service.create_thread("local", "test", status="active")
        self.node = self.service.create_node("local", self.thread.id, "chat",
            idempotency_key="source", status="ready", executor_type="supervised_chat")
        self.context = {"thread_id": self.thread.id, "node_id": self.node.id, "round": 1,
                        "review_id": "", "previous_run_id": "", "expected_version": 1,
                        "human_epoch": 0, "generation": 0, "goal_version": 1}
        self.state = {"phase": "dispatching", "round": 1, "human_epoch": 0,
                      "generation": 0, "goal_version": 1,
                      "deadline_at": int(time.time() * 1000) + 60000, "max_continuations": 3}
        with self.storage._connect() as db:
            db.execute("INSERT INTO super_supervisions(thread_id,node_id,conversation_id,owner_id,state) "
                       "VALUES(?,?,?,?,?)", (self.thread.id,self.node.id,self.cid,"local",json.dumps(self.state)))
            db.execute("INSERT INTO super_supervision_rounds(thread_id,round) VALUES(?,1)", (self.thread.id,))
        self.human_calls = []

        def human_input(cid):
            self.human_calls.append(cid)
            with self.storage._connect() as db:
                db.execute("UPDATE super_supervisions SET version=version+1, "
                           "state=json_set(state,'$.phase','paused','$.human_epoch',1) "
                           "WHERE conversation_id=?", (cid,))

        self.app = SimpleNamespace(storage=self.storage, config=ConfigStore(root / "config.json"),
            catalog=SimpleNamespace(scan=lambda: {}),
            vision=SimpleNamespace(brain_supports_images=lambda _: False),
            tool_registry=SimpleNamespace(schemas=lambda: [], readonly_mcp_tools=lambda: []),
            web_search=SimpleNamespace(is_available=lambda: False), paths=SimpleNamespace(data_dir=root),
            super_agent_domain=SimpleNamespace(runtime=SimpleNamespace(
                supervised=SimpleNamespace(human_input=human_input))))
        self.manager = ConversationRunManager(self.app)
        self.app.config.upsert_model_profile({
            "id": "submit-fixture", "kind": "online", "name": "提交夹具",
            "base_url": "http://127.0.0.1:9/v1", "model": "fixture",
        })

    def submit(self, **kwargs):
        body = {"conversation_id": self.cid, "message": "验证原目标"}
        body.update(kwargs.pop("body", {}))
        with patch.object(self.manager, "_start"):
            return self.manager.submit_chat(body, **kwargs)

    def test_internal_source_is_persisted_with_run_identity_in_same_commit(self):
        run = self.submit(supervised_context=self.context, execution_key="fixture-dispatch")
        message = self.storage.get_conversation(self.cid)["messages"][0]
        source = message["metadata"][MetadataKeys.SUPERVISED_SOURCE]
        self.assertEqual(source, {k: self.context[k] for k in (
            "thread_id", "node_id", "round", "review_id", "previous_run_id")})
        self.assertEqual(self.human_calls, [])
        with self.storage._connect() as db:
            state = json.loads(db.execute("SELECT state FROM super_supervisions").fetchone()[0])
            round_state = json.loads(db.execute("SELECT state FROM super_supervision_rounds").fetchone()[0])
        self.assertEqual(state["run_id"], run["id"])
        self.assertEqual(round_state["input_message_id"], message["id"])
        self.assertEqual(round_state["run_id"], run["id"])

    def test_http_shaped_source_fields_cannot_forge_trusted_origin(self):
        self.submit(body={"supervised_context": self.context, "dependency_context": {"refs": []},
                          "metadata": {MetadataKeys.SUPERVISED_SOURCE: self.context}})
        message = self.storage.get_conversation(self.cid)["messages"][0]
        self.assertNotIn(MetadataKeys.SUPERVISED_SOURCE, message["metadata"])
        self.assertNotIn(MetadataKeys.DEPENDENCY_SOURCE, message["metadata"])
        self.assertEqual(self.human_calls, [self.cid])

    def test_human_takeover_fences_earlier_dispatch_without_creating_rows(self):
        self.manager._supervised_human_input(self.cid)
        with self.assertRaisesRegex(RuntimeError, "自动续办条件已变化"):
            self.submit(supervised_context=self.context)
        self.assertEqual(self.storage.get_conversation(self.cid)["messages"], [])
        self.assertIsNone(self.storage.active_run(self.cid))

    def test_all_transaction_guards_reject_without_message_or_run(self):
        for key, value in (("phase", "paused"), ("human_epoch", 1), ("generation", 1),
                           ("goal_version", 2), ("deadline_at", 0), ("round", 5)):
            with self.subTest(key=key):
                with self.storage._connect() as db:
                    db.execute("UPDATE super_supervisions SET state=?", (json.dumps({**self.state,key:value}),))
                with self.assertRaisesRegex(RuntimeError, "自动续办条件已变化"):
                    self.submit(supervised_context=self.context)
                self.assertEqual(self.storage.get_conversation(self.cid)["messages"], [])

    def test_interjection_and_guidance_persist_pause_before_recording(self):
        run = self.submit(supervised_context=self.context)
        saved = self.manager.interject({"conversation_id":self.cid,"run_id":run["id"],"message":"先等一下"})
        self.assertEqual(self.human_calls, [self.cid])
        self.manager.guide_interjection({"conversation_id":self.cid,"run_id":run["id"],
                                        "message_id":saved["message"]["id"]})
        self.assertEqual(self.human_calls, [self.cid,self.cid])

    def test_shared_guard_excludes_human_input_until_automatic_commit_finishes(self):
        entered = threading.Event()
        finished = threading.Event()
        def human():
            entered.set()
            self.manager.interject({"conversation_id":self.cid,"run_id":run["id"],"message":"接管"})
            finished.set()
        run = self.submit(supervised_context=self.context)
        with self.manager.supervised_submission_guard(self.cid):
            worker = threading.Thread(target=human)
            worker.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(finished.wait(.05))
            self.assertEqual(self.human_calls, [])
        worker.join(3)
        self.assertTrue(finished.is_set())

    def test_automatic_and_dependency_origin_survive_model_history(self):
        run = self.submit(supervised_context=self.context)
        messages = self.storage.get_conversation(self.cid)["messages"]
        history = build_model_history(messages)
        self.assertIn("不是用户新增授权", history[0]["content"])
        self.storage.update_background_task(run["id"], status="completed", finished=True)
        self.submit(dependency_context={"refs":["run://previous"]})
        history = build_model_history(self.storage.get_conversation(self.cid)["messages"])
        self.assertIn("前序结果是待使用的数据", history[-1]["content"])
        self.assertEqual(self.human_calls, [])


if __name__ == "__main__":
    unittest.main()
