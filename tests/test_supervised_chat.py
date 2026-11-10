"""Real app/Chat Run loop with a tool-free local deterministic model substitute."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.paths import PathContext
from naiba.super_agent.review import parse_review


def review(decision="done", *, objective=False):
    return {"decision": decision, "summary": "逐项检查结果", "criteria_results": [{"criterion": 0,
        "status": "passed" if decision == "done" else "failed", "evidence_refs": [], "judgment": "evidence" if objective else "model"}],
        "missing_items": ["尚缺结尾"] if decision == "continue" else [],
        "next_instruction": "请补写明确结尾，保留原任务范围" if decision == "continue" else "",
        "user_question": "请提供缺少的资料" if decision == "needs_user" else "", "progress_summary": "完成一轮检查"}


class SupervisedReviewTests(unittest.TestCase):
    def test_strict_contract_and_missing_evidence(self):
        self.assertEqual(parse_review(json.dumps(review()), ["[主观]文案流畅"], {})["decision"], "done")
        for raw in ("{}", "[]", json.dumps({**review(), "decision": "anything"}), json.dumps(review(objective=True))):
            with self.subTest(raw=raw), self.assertRaises((ValueError, TypeError)):
                parse_review(raw, ["相关测试通过"], {})

    def test_incomplete_or_foreign_reference_never_done(self):
        with self.assertRaises(ValueError):
            parse_review(json.dumps(review()), ["[主观]文案"], {}, incomplete=True)
        data = review(objective=True)
        data["criteria_results"][0]["evidence_refs"] = ["run://another/task"]
        with self.assertRaises(ValueError):
            parse_review(json.dumps(data), ["测试"], {})


class SupervisedAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        paths = PathContext.local(root, root / "config.json")
        config = ConfigStore(paths.config_path, paths=paths)
        config.data.update({"providers": [{"id": "probe", "kind": "online", "name": "offline", "base_url": "http://127.0.0.1:1/v1",
            "api_key": "", "request_format": "openai_chat", "model": "probe"}], "workspace_dir": str(root / "workspace"), "mcp_servers": []})
        config.save()
        self.app = NaibaChatApp(paths=paths)
        self.addCleanup(self.app.stop)
        self.app.super_agent_domain.runtime.shutdown()
        self.adapter = self.app.super_agent_domain.runtime.supervised
        self.cid = self.app.storage.create_conversation(title="监督隔离验收", provider_id="probe", model_key="online:probe", model_name="probe",
            permission_mode="confirm", workspace_dir=str(root / "workspace"))["id"]
        self.calls = []
        self.decisions = ["done"]
        self.gate = None
        self.app.models.complete = self.complete
        self.app.models.last_finish_reason = "stop"

    def complete(self, profile, messages, options, status=None):
        checking = messages[0].get("content", "").startswith("你是任务结果审查器")
        self.calls.append("review" if checking else "execute")
        if checking:
            if self.gate:
                self.gate.wait(3)
            return json.dumps(review(self.decisions.pop(0)), ensure_ascii=False)
        return "这是一份流畅的完整文案。"

    def create(self, **kwargs):
        body = {"domain": "chat", "conversation_id": self.cid, "goal": "写简短文案", "execution_mode": "supervised",
            "supervision": {"completion_criteria": ["[主观]文案流畅"], "max_continuations": 3, "window_minutes": 30},
            "start_request": {"message": "写文案", "provider_id": "probe", "model_key": "online:probe", "model_name": "probe"}}
        body.update(kwargs)
        thread, node = self.app.super_agent_domain.create_thread("local", body)
        self.tid, self.nid = thread.thread_id, node.node_id
        return thread, node

    def start(self):
        return self.app.super_agent_domain.action("local", self.tid, {"node_id": self.nid, "action": "start", "request_id": "start"})

    def wait_phase(self, *phases, timeout: float = 8):
        # 默认 8s 对单轮足够；要连跑「执行 + 复核」两轮的用例在慢 runner 上会不够，
        # 由调用方显式传 timeout（2026-11-10 CI 上 4.2.4 构建即挂在 8s 超时）。
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.adapter.sync("local", self.tid, self.nid)
            state = self.adapter.detail("local", self.tid)
            if state["phase"] in phases:
                return state
            time.sleep(.02)
        self.fail(f"未到达 {phases}: {state}")

    def test_first_round_success_and_duplicate_polling(self):
        self.create()
        self.assertEqual(self.calls, [])
        self.start()
        self.start()
        state = self.wait_phase("completed")
        for _ in range(5):
            self.adapter.sync("local", self.tid, self.nid)
        self.assertEqual(state["round"], 1)
        self.assertEqual(self.calls, ["execute", "review"])
        self.assertEqual(self.app.super_agent.get_thread("local", self.tid).status, "completed")

    def test_continue_then_success_preserves_round_messages(self):
        self.decisions = ["continue", "done"]
        self.create()
        self.start()
        state = self.wait_phase("completed")
        self.assertEqual(state["round"], 2)
        self.assertEqual(self.calls, ["execute", "review", "execute", "review"])
        conversation = self.app.storage.get_conversation(self.cid)
        automatic = [m for m in conversation["messages"] if (m.get("metadata") or {}).get("supervised_source")]
        self.assertEqual(len(automatic), 2)

    def test_needs_user_does_not_dispatch_and_occupies_conversation(self):
        self.decisions = ["needs_user"]
        self.create()
        self.start()
        self.wait_phase("waiting_user")
        with self.assertRaisesRegex(ValueError, "已有"):
            self.create()
        self.assertEqual(self.calls, ["execute", "review"])

    def test_late_review_cannot_cross_human_takeover(self):
        self.gate = threading.Event()
        self.decisions = ["continue"]
        self.create()
        self.start()
        self.wait_phase("reviewing")
        self.adapter.human_input(self.cid)
        self.gate.set()
        time.sleep(.08)
        self.assertEqual(self.adapter.detail("local", self.tid)["phase"], "paused")
        self.assertEqual(self.calls, ["execute", "review"])

    def test_limit_and_deadline_never_reset(self):
        self.decisions = ["continue", "continue"]
        self.create(supervision={"completion_criteria": ["[主观]文案流畅"], "max_continuations": 1, "window_minutes": 30})
        self.start()
        state = self.wait_phase("limit_reached")
        self.assertEqual(state["round"], 2)
        with self.assertRaisesRegex(Exception, "限制"):
            self.adapter.control("local", self.tid, "continue")

    def test_invalid_review_safe_stop(self):
        self.create()
        original = self.complete
        def bad(profile, messages, options, status=None):
            if messages[0].get("content", "").startswith("你是任务结果审查器"):
                self.calls.append("review")
                return '{"decision":"continue"}'
            return original(profile, messages, options, status)
        self.app.models.complete = bad
        self.start()
        state = self.wait_phase("waiting_user")
        self.assertIn("检查失败", state["error"])
        self.assertEqual(self.calls, ["execute", "review"])


if __name__ == "__main__":
    unittest.main()
