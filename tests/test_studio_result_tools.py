"""The real shared agent loop must write and confirm every new workbench result."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from naiba.studio.jobs import StudioJobManager
from naiba.studio.runner import StudioRunner
from naiba.studio.store import StudioStore
from naiba.studio.model_identity import configured_identity
from tests.studio_result_fixture import complete_result_reply, write_result_reply, latest_result_receipt


class Models:
    def __init__(self):
        self.calls = []
        self.last_usage = {"input_tokens": 10, "output_tokens": 4}
        self.last_finish_reason = "tool_calls"
        self.last_reasoning = ""
        self.content = " 正文😀\r\n下一行 "
        self.behavior = None

    def complete(self, profile, messages, options, event):
        self.calls.append(copy.deepcopy(messages))
        self.last_usage = {"input_tokens": 10, "output_tokens": 4}
        self.last_finish_reason = "tool_calls"
        if self.behavior:
            return self.behavior(messages, options, event)
        return complete_result_reply(messages) or write_result_reply(self.content)


class ResultGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.models = Models()
        config = SimpleNamespace(profile=lambda _key: {"kind": "online", "model": "fixture", "context_window": 64000},
                                 runtime_guard_options=lambda: {})
        self.app = SimpleNamespace(paths=SimpleNamespace(data_dir=self.root), config=config, models=self.models)
        self.store = StudioStore(self.root)
        self.runner = StudioRunner(self.app, self.store)
        self.addCleanup(self.runner.shutdown)
        self.jobs = StudioJobManager(self.app, self.store, self.runner)

    def run_task(self, **extra):
        data = {"module": "novel", "content": "生成正文", "model_key": "online:test", "model_name": "fixture", **extra}
        data["model_snapshot"] = configured_identity(data, self.app.config.profile(data["model_key"]))
        task = self.store.create_task(data)
        self.runner._run_task(task["id"])
        return self.jobs.task(task["id"])

    def test_plain_path_uses_tools_and_stops_after_complete(self):
        result = self.run_task()
        self.assertEqual(result["status"], "done", result["error"])
        self.assertEqual(result["result"], self.models.content)
        self.assertEqual(len(self.models.calls), 2)
        self.assertEqual(result["result_artifact"]["status"], "final")
        self.assertEqual(self.jobs.result_text(result["id"]), self.models.content)
        self.assertGreaterEqual(len(result["trace_messages"]), 5)

    def test_chunked_path_creates_each_block_and_exact_separator(self):
        result = self.run_task(chunked={"blocks": ["一", "二"], "continuity_chars": 8})
        self.assertEqual(result["status"], "done", result["error"])
        self.assertEqual(result["result"], self.models.content + "\n\n" + self.models.content)
        self.assertEqual(result["result_artifact"]["completed_blocks"], 2)
        self.assertEqual(len(self.models.calls), 4)

    def test_ordinary_completion_cannot_become_body(self):
        self.models.behavior = lambda *_args: "写好了"
        result = self.run_task()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"], "")
        self.assertEqual(result["result_partial"], "")
        self.assertEqual(len(self.models.calls), 3)

    def test_network_failure_keeps_only_confirmed_content_and_resume(self):
        def failure(messages, options, event):
            if complete_result_reply(messages):
                raise RuntimeError("断开连接")
            return write_result_reply("已确认半篇")
        self.models.behavior = failure
        result = self.run_task()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result_partial"], "已确认半篇")
        child = self.jobs.prepare_resume(result["id"], request_key="resume")
        def resumed(messages, options, event):
            receipt = complete_result_reply(messages)
            return receipt or write_result_reply("补齐后半", segment=2)
        self.models.behavior = resumed
        self.runner._run_task(child["id"])
        finished = self.jobs.task(child["id"])
        self.assertEqual(finished["status"], "done", finished["error"])
        self.assertEqual(finished["result"], "已确认半篇补齐后半")

    def test_failed_request_reports_usage_once_without_reusing_previous_usage(self):
        for source in ("side-channel", "event", "unknown"):
            with self.subTest(source=source):
                def failure(messages, options, event):
                    if complete_result_reply(messages):
                        self.models.last_usage = {}
                        self.models.last_finish_reason = ""
                        if source == "side-channel":
                            self.models.last_usage = {"input_tokens": 20, "output_tokens": 1}
                        elif source == "event":
                            event({"type": "usage", "usage": {"input_tokens": 20, "output_tokens": 1}})
                        raise RuntimeError("usage之后断开")
                    return write_result_reply("确认段")
                self.models.behavior = failure
                result = self.run_task()
                self.assertEqual(result["status"], "failed")
                expected = 10 if source == "unknown" else 30
                self.assertEqual(result["usage"]["input_tokens"], expected)
                self.assertEqual(result["usage"]["requests"], 2)
                records = result["usage"]["records"]
                self.assertEqual(len(records), 2)
                self.assertEqual(records[1], {} if source == "unknown" else {"input_tokens": 20, "output_tokens": 1})
                attempts = self.store.artifacts.manifest(self.store.get_task(result["id"]))["attempts"]
                self.assertTrue(attempts[-1]["failed"])
                self.assertEqual(attempts[-1]["finish_reason"], "unknown")
                self.assertEqual(attempts[-1]["usage_known"], source != "unknown")

    def test_final_snapshot_resume_does_not_call_model(self):
        result = self.run_task()
        self.store.update_task(result["id"], status="interrupted")
        child = self.jobs.prepare_resume(result["id"], request_key="recover-final")
        before = len(self.models.calls)
        self.runner._run_task(child["id"])
        self.assertEqual(len(self.models.calls), before)
        finished = self.jobs.task(child["id"])
        self.assertEqual(finished["status"], "done")
        self.assertEqual(finished["usage"], {})

    def test_skill_path_retains_frozen_read_tools_and_result_tools(self):
        from naiba.skills.catalog import SkillCatalog
        from naiba.studio.content import ContentSnapshots
        folder = self.root / "skills" / "one"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: one\n---\n保持冻结规则。", encoding="utf-8")
        catalog = SkillCatalog([folder.parent])
        skill_id = catalog.scan()[0]["id"]
        frozen = ContentSnapshots(self.root).normalize_and_freeze({
            "module": "novel", "content": "生成正文", "model_key": "online:test", "model_name": "fixture",
            "prompt_source": "skill", "skill_ids": [skill_id]}, catalog)
        task = self.store.create_task(frozen)
        self.runner._run_task(task["id"])
        result = self.jobs.task(task["id"])
        self.assertEqual(result["status"], "done", result["error"])
        self.assertEqual(result["result"], self.models.content)
        self.assertIn("保持冻结规则", self.models.calls[0][0]["content"])
        self.assertEqual(len(self.models.calls), 2)

    def test_length_cannot_publish_final_until_changed_followup(self):
        calls = 0
        def generate(messages, options, event):
            nonlocal calls
            calls += 1
            self.models.last_finish_reason = "length" if calls == 2 else "tool_calls"
            return complete_result_reply(messages) or write_result_reply("确认正文")
        self.models.behavior = generate
        result = self.run_task()
        self.assertEqual(result["status"], "done", result["error"])
        self.assertEqual(len(self.models.calls), 3)
        self.assertEqual(result["result"], "确认正文")
        attempts = self.store.artifacts.manifest(self.store.get_task(result["id"]))["attempts"]
        self.assertEqual([row["finish_reason"] for row in attempts], ["tool_calls", "length", "tool_calls"])
        self.assertEqual(result["usage"]["input_tokens"], 30)
        self.assertEqual(result["usage"]["requests"], 3)

    def test_skill_reference_read_remains_after_many_acknowledged_output_groups(self):
        from naiba.skills.catalog import SkillCatalog
        from naiba.studio.content import ContentSnapshots
        folder = self.root / "skills" / "reference"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: reference\n---\n读取参考再创作。", encoding="utf-8")
        reference = folder / "reference.md"
        reference.write_text("REFERENCE_MUST_STAY：人物关系和阶段约束不得丢弃。", encoding="utf-8")
        catalog = SkillCatalog([folder.parent])
        frozen = ContentSnapshots(self.root).normalize_and_freeze({
            "module": "novel", "content": "生成正文", "model_key": "online:test", "model_name": "fixture",
            "prompt_source": "skill", "skill_ids": [catalog.scan()[0]["id"]]}, catalog)
        def generate(messages, options, event):
            read = any(row.get("role") == "tool" and row.get("name") == "read_file" for row in messages)
            if not read:
                self.assertEqual(len(self.models.calls), 1, "参考读取组被错误淘汰后会重复读")
                return json.dumps({"type": "tool", "tool": "read_file", "arguments": {"path": str(reference)}})
            self.assertIn("REFERENCE_MUST_STAY", json.dumps(messages, ensure_ascii=False))
            receipt = latest_result_receipt(messages)
            count = receipt["segments"] if receipt else 0
            if count == 5:
                return complete_result_reply(messages)
            return write_result_reply(f"第{count + 1}段。", segment=count + 1)
        self.models.behavior = generate
        task = self.store.create_task(frozen)
        self.runner._run_task(task["id"])
        result = self.jobs.task(task["id"])
        self.assertEqual(result["status"], "done", result["error"])
        self.assertEqual(len(self.models.calls), 7)
        self.assertTrue(all("REFERENCE_MUST_STAY" in json.dumps(messages, ensure_ascii=False)
                            for messages in self.models.calls[1:]))
        reads = [call for row in result["trace_messages"] for call in row.get("tool_calls", [])
                 if call.get("name") == "read_file"]
        self.assertEqual(len(reads), 1)

    def test_long_output_uses_bounded_view_but_keeps_every_tool_argument_in_trace(self):
        self.app.config.profile = lambda _key: {"kind": "online", "model": "fixture", "context_window": 8000}
        text = "中文续写" * 250
        segments = 40
        def generate(messages, options, event):
            receipt = latest_result_receipt(messages)
            count = receipt["segments"] if receipt else 0
            if count == segments:
                return complete_result_reply(messages)
            return write_result_reply(text, segment=count + 1)
        self.models.behavior = generate
        result = self.run_task()
        self.assertEqual(result["status"], "done", result["error"])
        self.assertEqual(result["result"], text * segments)
        self.assertEqual(len(self.models.calls), segments + 1)
        self.assertLess(max(len(json.dumps(rows, ensure_ascii=False)) for rows in self.models.calls), 20000)
        trace_writes = [call for row in result["trace_messages"] for call in row.get("tool_calls", [])
                        if call.get("name") == "write_result_txt"]
        self.assertEqual(len(trace_writes), segments)
        self.assertTrue(all(call["arguments"]["text"] == text for call in trace_writes))

    def test_json_segments_remain_exact_and_resume_state_tracks_open_string(self):
        from naiba.studio.result_tools import _json_continuation_state
        first = '{"章节":[{"正文":"第一行\\n'
        second = '第二行😀"}]}\r\n '
        def generate(messages, options, event):
            receipt = latest_result_receipt(messages)
            if receipt is None:
                return write_result_reply(first)
            if receipt["segments"] == 1:
                return write_result_reply(second, segment=2)
            return complete_result_reply(messages)
        self.models.behavior = generate
        result = self.run_task(module="writer")
        self.assertEqual(result["status"], "done", result["error"])
        self.assertEqual(result["result"], first + second)
        self.assertEqual(json.loads(result["result"])["章节"][0]["正文"], "第一行\n第二行😀")
        state = _json_continuation_state(first)
        self.assertTrue(state["in_string"])
        self.assertEqual(state["open_depth"], 3)

    def test_confirmed_cancel_after_final_file_never_turns_task_done(self):
        original = self.store.artifacts.complete_block
        def complete_then_cancel(task, block_no, receipt, **kwargs):
            result = original(task, block_no, receipt, **kwargs)
            kwargs["cancel"].set()
            return result
        with patch.object(self.store.artifacts, "complete_block", side_effect=complete_then_cancel):
            result = self.run_task()
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn("done", [row["type"] for row in self.jobs.events(result["id"])])
        self.assertEqual(self.store.artifacts.read(self.store.get_task(result["id"]))[0], self.models.content)

    def test_every_registered_workbench_generation_step_gets_file_contract(self):
        stages = {"novel": ["elements", "outline", "script", "adapt", "storyboard"],
                  "writer": ["genre", "title", "blurb", "world", "chars", "outline", "outline_text",
                             "outline_review", "split", "segs", "draft", "state"]}
        for module, steps in stages.items():
            for step in steps:
                with self.subTest(module=module, step=step):
                    result = self.run_task(module=module, **{module: {"stage" if module == "novel" else "step": step}})
                    self.assertEqual(result["status"], "done", result["error"])
                    self.assertEqual(result["result_artifact"]["status"], "final")


if __name__ == "__main__":
    unittest.main()
