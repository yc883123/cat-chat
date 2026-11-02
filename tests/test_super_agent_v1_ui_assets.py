"""Deterministic asset/fixture checks only; these tests never launch a browser."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v1_ui_fixture", ROOT / "verify" / "_serve_super_agent_v1_ui.py")
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


class FixtureTaskTests(unittest.TestCase):
    def test_only_studio_task_fails_once_then_recovers(self):
        jobs = FIXTURE.DeterministicStudioJobs()
        request = {"model_name": "smoke-fail", "writer": {"book_id": "book", "step": "genre"}}
        first = jobs.submit(request)
        second = jobs.submit(request)
        self.assertEqual((first["status"], second["status"]), ("failed", "done"))
        self.assertEqual(json.loads(second["result"])["channel"], "male")
        request["writer"]["step"] = "changed"
        self.assertEqual(second["input"]["writer"]["step"], "genre")

    def test_browser_asset_has_all_viewports_and_no_route_fulfillment(self):
        text = (ROOT / "verify" / "super_agent_v1_ui_smoke.cjs").read_text(encoding="utf-8")
        for marker in ("width: 1280", "width: 375", "checkNoScriptError", "domain, 'mixed'", "stage_promoted", "approval_task", "autostart", "'AddStep'"):
            self.assertIn(marker, text)
        self.assertNotIn("route.fulfill", text)
        self.assertNotIn("rmSync", text)


class FixtureHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="naiba_v1_asset_")
        cls.app, cls.server, cls.model, cls.fixture, cls.state = FIXTURE.build_app(Path(cls.tmp.name) / "isolated")
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.worker.join(timeout=5)
        cls.server.server_close()
        cls.app.stop()
        cls.model.shutdown()
        cls.model.server_close()
        cls.tmp.cleanup()

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.fixture["base"] + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.load(response)

    def test_real_catalog_lists_nonfirst_default_and_fixture_books(self):
        status, result = self.request("/api/providers/models", {"model_key": "online:probe"})
        self.assertEqual(status, 200)
        self.assertIn("probe", [item["id"] for item in result["models"]])
        self.assertNotEqual(result["models"][0]["id"], "probe")
        for domain, key in (("writer", "writer_book_id"), ("novel", "novel_book_id")):
            _, result = self.request(f"/api/{domain}/books/{self.fixture[key]}")
            self.assertTrue(result["chapters"])
            if domain == "writer":
                self.assertEqual(len(result["outline"]["parts"]), 2)

    def test_real_http_mixed_creation_never_dispatches_chat_or_subagent(self):
        selection = {"provider_id": "probe", "model_key": "online:probe", "model_name": "probe"}
        cid = self.fixture["conversation_id"]
        nodes = [
            {"key": "chat", "domain": "chat", "conversation_id": cid, **selection, "start_request": {"message": "create only"}},
            {"key": "subagent", "domain": "subagent", "conversation_id": cid, **selection, "start_request": {"instruction": "create only"}},
            {"key": "writer", "domain": "writer", "book_id": self.fixture["writer_book_id"], "stage": "chapter", **selection, "start_request": {"kind": "chapter", "chapter_no": 1}},
            {"key": "novel", "domain": "novel", "book_id": self.fixture["novel_book_id"], "stage": "elements", **selection, "start_request": {"chapter_ids": [1, 2]}},
            {"key": "job", "domain": "job", "conversation_id": cid, "job_id": self.fixture["job_id"]},
        ]
        status, result = self.request("/api/super-agent/threads", {"domain": "mixed", "nodes": nodes, "chain_instance_id": "asset-mixed", "autostart": False, "auto_advance": False})
        self.assertEqual(status, 201)
        self.assertEqual(len(result["nodes"]), 5)
        self.assertEqual(result["nodes"][0]["status"], "ready")
        self.assertTrue(all(node["status"] == "blocked" for node in result["nodes"][1:]))
        self.assertEqual(self.state.model_requests, [])
        self.assertTrue(all(not node.get("executor_id") for node in result["nodes"]))
        stored = self.app.super_agent.list_nodes("local", result["thread"]["thread_id"])
        self.assertTrue(all(not node.executor_id for node in stored))

    def test_refuses_reusing_nonempty_fixture_directory(self):
        with self.assertRaisesRegex(ValueError, "new empty directory"):
            FIXTURE.build_app(Path(self.tmp.name) / "isolated")


if __name__ == "__main__":
    unittest.main()
