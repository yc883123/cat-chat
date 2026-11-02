from pathlib import Path
import tempfile
import unittest

from naiba.storage.store import ChatStorage
from naiba.super_agent.service import SuperAgentService


class RecipeCollectionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="recipe-projection-")
        self.addCleanup(self.tmp.cleanup)
        self.service = SuperAgentService(ChatStorage(Path(self.tmp.name) / "chat.db"))

    def completed(self, executor="recipe_batch", batch_id="recipe-test"):
        thread, nodes = self.service.create_thread_chain(
            "local", "Collection", "Missing file recovery", chain_instance_id=batch_id,
            chain_version=1, nodes=[{"kind": "collection", "executor_type": executor,
                                     "executor_id": batch_id, "stage_key": "collection"}],
        )
        node = nodes[0]
        self.service.store.update_thread(thread.thread_id, owner_id="local", status="active")
        self.service.store.update_node(node.node_id, owner_id="local", status="running")
        self.service.store.update_node(node.node_id, owner_id="local", status="succeeded")
        self.service.store.update_thread(thread.thread_id, owner_id="local", status="completed")
        return thread, node

    def test_recipe_collection_only_reopens_exact_owned_projection(self):
        thread, node = self.completed()
        self.service.store.reopen_recipe_collection(owner_id="local", thread_id=thread.thread_id,
                                                   node_id=node.node_id, batch_id="recipe-test")
        self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "waiting_user")
        self.assertEqual(self.service.get_node("local", node.node_id).status, "waiting_user")

    def test_non_recipe_or_wrong_identity_cannot_reopen(self):
        for executor, batch in (("chat_run", "chat-test"), ("recipe_batch", "recipe-test")):
            with self.subTest(executor=executor):
                thread, node = self.completed(executor, batch)
                with self.assertRaises(ValueError):
                    self.service.store.reopen_recipe_collection(owner_id="local", thread_id=thread.thread_id,
                                                               node_id=node.node_id, batch_id="wrong" if executor == "recipe_batch" else batch)
                with self.assertRaises(LookupError):
                    self.service.store.reopen_recipe_collection(owner_id="other", thread_id=thread.thread_id,
                                                               node_id=node.node_id, batch_id=batch)
                self.assertEqual(self.service.get_thread("local", thread.thread_id).status, "completed")
                self.assertEqual(self.service.get_node("local", node.node_id).status, "succeeded")
                with self.assertRaises(RuntimeError):
                    self.service.store.update_thread(thread.thread_id, owner_id="local", status="active")


if __name__ == "__main__":
    unittest.main()
