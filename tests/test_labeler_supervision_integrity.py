"""并发监督的规则、最终交付物、费用与缓存作者必须同源。"""
from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from naiba.core.contracts import (LABELER_APPROVED_TXT_KEY, LABELER_UNKNOWN_WORKER_SLOT,
                                  LABELER_SUPERVISION_RULES_KEY)
from naiba.core.exceptions import TaskCancelled
from naiba.labeler.db import LabelerDB
from naiba.labeler.engines.chat import ChatSkillEngine
from naiba.labeler.runner import LabelerRunner
from naiba.labeler.gate import ProviderGate
from naiba.llm.runtime import ModelRuntime
from naiba.labeler.supervisor import MAX_SUPERVISION_TXT_CHARS
from naiba.labeler.txt_output import TXT_TOOL_NAME, TxtToolContext
from tests import test_labeler as serial_fixtures
from tests import test_labeler_concurrency as concurrent_fixtures
from tests.test_labeler import _StubModels
from tests.test_labeler_concurrency import (
    _ConcurrencyModels, _StubApp, _workers,
    _candidate, _image_name, _message_text,
)


class SupervisionIntegrityTests(unittest.TestCase):
    setUp = concurrent_fixtures.ConcurrentRunnerTests.setUp
    _jobs = concurrent_fixtures.ConcurrentRunnerTests._jobs
    _images = concurrent_fixtures.ConcurrentRunnerTests._images
    _submit = concurrent_fixtures.ConcurrentRunnerTests._submit
    _wait = concurrent_fixtures.ConcurrentRunnerTests._wait

    @staticmethod
    def tool_label(text, _options, index):
        if index < 2:
            return json.dumps({"type": "tool", "tool": TXT_TOOL_NAME, "arguments": {
                "item": _image_name(text), "mode": "write" if index == 0 else "append",
                "content": "dog\n" if index == 0 else "airplane",
            }})
        return '{"tags":[{"tag":"cat","confidence":0.9}],"description":"一只猫"}'

    def test_supervisor_sees_exact_multisegment_txt_and_cache_preserves_it(self):
        seen = []

        def supervise(text, options, index):
            seen.append(_candidate(text))
            return '{"decision":"approve","reason":"ok"}'

        models = _ConcurrencyModels(label=self.tool_label, supervisor=supervise)
        app = _StubApp(self.data_dir, models=models)
        images = self._images(1)
        _, task = self._submit(app, images, workers=_workers(1), write_txt=True)
        self.assertEqual(self._wait(task["id"])["status"], "done")
        self.assertEqual(seen[0]["tags"], ["cat"])
        self.assertEqual(seen[0]["txt_content"], "dog\nairplane")
        row = self.db.list_items(task["id"])[0]
        self.assertEqual((self.store.outputs_dir(task["id"]) / row["txt_path"]).read_text(encoding="utf-8"), "dog\nairplane")
        annotation = self.db.annotations(task_id=task["id"])[0]
        self.assertEqual(annotation[LABELER_APPROVED_TXT_KEY], "dog\nairplane")
        calls = dict(models.counters)
        _, repeated = self._submit(app, images, workers=_workers(1), write_txt=True)
        self.assertEqual(self._wait(repeated["id"])["status"], "done")
        self.assertEqual(models.counters, calls)
        cached = self.db.list_items(repeated["id"])[0]
        self.assertEqual((self.store.outputs_dir(repeated["id"]) / cached["txt_path"]).read_text(encoding="utf-8"), "dog\nairplane")

    def test_cancelled_supervision_never_exposes_provisional_txt(self):
        entered = threading.Event()

        def supervise(text, options, index):
            entered.set()
            self.assertTrue(options["cancel_event"].wait(5))
            raise TaskCancelled("任务已取消")

        models = _ConcurrencyModels(supervisor=supervise)
        app = _StubApp(self.data_dir, models=models)
        jobs, task = self._submit(app, self._images(1), workers=_workers(1), write_txt=True)
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.db.list_items(task["id"])[0]["txt_state"], "")
        self.assertEqual(list(self.store.outputs_dir(task["id"]).rglob("*.txt")), [])
        self.assertTrue(jobs.cancel(task["id"]))
        self.assertEqual(self._wait(task["id"])["status"], "cancelled")
        self.assertEqual(self.db.list_items(task["id"])[0]["txt_state"], "")
        self.assertEqual(list(self.store.outputs_dir(task["id"]).rglob("*.txt")), [])

    def test_cancelled_late_invalid_or_revise_verdict_stays_cancelled(self):
        for reply in ("not JSON", '{"decision":"revise"}'):
            with self.subTest(reply=reply):
                entered, release = threading.Event(), threading.Event()
                original = LabelerRunner._supervisor_request
                def held(runner, **kwargs):
                    result = original(runner, **kwargs)
                    entered.set()
                    self.assertTrue(release.wait(5))
                    return result
                models = _ConcurrencyModels(supervisor=lambda *_: reply)
                app = _StubApp(self.data_dir, models=models)
                with patch.object(LabelerRunner, "_supervisor_request", held):
                    jobs, task = self._submit(app, self._images(1), workers=_workers(1), write_txt=True)
                    self.assertTrue(entered.wait(5))
                    self.assertTrue(jobs.cancel(task["id"]))
                    release.set()
                    final = self._wait(task["id"])
                row = self.db.list_items(task["id"])[0]
                self.assertEqual(final["status"], "cancelled")
                self.assertEqual(row["state"], "cancelled")
                self.assertEqual(final["usage"]["total_tokens"], models.total_tokens())
                self.assertEqual(row["usage"]["total_tokens"], models.total_tokens())

    def test_cancellation_acknowledged_before_late_approval_prevents_publish(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = LabelerRunner._supervisor_request

        def held(runner, **kwargs):
            result = original(runner, **kwargs)
            entered.set()
            self.assertTrue(release.wait(5))
            return result

        models = _ConcurrencyModels()
        app = _StubApp(self.data_dir, models=models)
        with patch.object(LabelerRunner, "_supervisor_request", held):
            jobs, task = self._submit(app, self._images(1), workers=_workers(1), write_txt=True)
            self.assertTrue(entered.wait(5))
            self.assertTrue(jobs.cancel(task["id"]))
            release.set()
            self.assertEqual(self._wait(task["id"])["status"], "cancelled")
        self.assertEqual(self.db.annotations(task_id=task["id"]), [])
        self.assertEqual(list(self.store.outputs_dir(task["id"]).rglob("*.txt")), [])

    def test_unparseable_review_preserves_candidate_txt_but_does_not_publish(self):
        models = _ConcurrencyModels(label=self.tool_label, supervisor=lambda *_: "not JSON")
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(1), workers=_workers(1), write_txt=True)
        self.assertEqual(self._wait(task["id"])["status"], "failed")
        row = self.db.list_items(task["id"])[0]
        self.assertEqual(row["state"], "human_check")
        self.assertEqual(row["candidate"]["txt_content"], "dog\nairplane")
        self.assertEqual(row["txt_state"], "")
        self.assertEqual(list(self.store.outputs_dir(task["id"]).rglob("*.txt")), [])

    def test_oversized_txt_is_explicit_human_check_not_truncated_approval(self):
        def label(text, options, index):
            if index < 4:
                return json.dumps({"type": "tool", "tool": TXT_TOOL_NAME, "arguments": {
                    "item": _image_name(text), "mode": "write" if index == 0 else "append",
                    "content": "x" * 16000,
                }})
            return '{"tags":["cat"],"description":""}'

        models = _ConcurrencyModels(label=label)
        _, task = self._submit(_StubApp(self.data_dir, models=models), self._images(1), workers=_workers(1), write_txt=True)
        self.assertEqual(self._wait(task["id"])["status"], "failed")
        row = self.db.list_items(task["id"])[0]
        self.assertEqual(row["state"], "human_check")
        self.assertEqual(len(row["candidate"]["txt_content"]), 64000)
        self.assertIn(str(MAX_SUPERVISION_TXT_CHARS), row["supervisor_reason"])
        self.assertEqual(models.counters["supervisor"], 0)
        self.assertEqual(list(self.store.outputs_dir(task["id"]).rglob("*.txt")), [])

    def test_format_failure_accounts_supervisor_tokens(self):
        models = _ConcurrencyModels(supervisor=lambda *_: "not JSON")
        _, task = self._submit(_StubApp(self.data_dir, models=models), self._images(1), workers=_workers(1))
        final = self._wait(task["id"])
        row = self.db.list_items(task["id"])[0]
        self.assertEqual(final["usage"]["total_tokens"], models.total_tokens())
        self.assertEqual(row["usage"]["total_tokens"], models.total_tokens())
        self.assertEqual(row["supervisor_usage"]["total_tokens"], 1001)

    def test_failed_publication_preserves_usage_and_candidate_without_success(self):
        models = _ConcurrencyModels(label=self.tool_label)
        with patch.object(TxtToolContext, "_write_text", side_effect=OSError("disk full")):
            _, task = self._submit(_StubApp(self.data_dir, models=models), self._images(1), workers=_workers(1), write_txt=True)
            final = self._wait(task["id"])
        row = self.db.list_items(task["id"])[0]
        self.assertEqual(final["status"], "failed")
        self.assertEqual(row["error_category"], "publish_failed")
        self.assertEqual(row["candidate"]["txt_content"], "dog\nairplane")
        self.assertEqual(final["usage"]["total_tokens"], models.total_tokens())
        self.assertEqual(self.db.annotations(task_id=task["id"]), [])
        self.assertEqual(list(self.store.outputs_dir(task["id"]).rglob("*.txt")), [])

    def test_cancel_at_retry_boundary_carries_previous_attempt_usage(self):
        class BoundaryCancel(threading.Event):
            def wait(self, timeout=None):
                self.set()
                return False  # 模拟退避刚结束，下一轮入口前取消。

        runner = self._jobs(_StubApp(self.data_dir, models=_ConcurrencyModels())).runner
        failed = RuntimeError("429 rate limit")
        failed.supervisor_usage = {"total_tokens": 19}
        with patch.object(runner, "_supervisor_request", side_effect=failed) as request:
            with self.assertRaises(TaskCancelled) as raised:
                runner._supervise_with_retry(task_id="probe", item={}, config={}, annotation={},
                    supervisor_profile={}, supervisor_ref={}, worker_slot=0, worker_model="m",
                    cancel_event=BoundaryCancel(), gate=ProviderGate(), timeout_seconds=30)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(raised.exception.supervisor_usage, {"total_tokens": 19})

    def test_real_runtime_failure_does_not_reuse_previous_review_usage(self):
        runtime = ModelRuntime()
        app = _StubApp(self.data_dir, models=runtime)
        runner = self._jobs(app).runner
        image = self._images(1)[0]
        kwargs = dict(task_id="probe", item={"path": str(image)},
            config={LABELER_SUPERVISION_RULES_KEY: "只描述主体"}, annotation={"tags": ["cat"]},
            supervisor_profile=app.config.profile("online:judge"),
            supervisor_ref={"model_key": "online:judge"}, worker_slot=0, worker_model="m",
            cancel_event=threading.Event(), gate=ProviderGate(), timeout_seconds=30)
        with patch.object(runtime, "_complete_online", side_effect=[
            ('{"decision":"approve"}', "", "", {"total_tokens": 19}), RuntimeError("connection failed")]):
            self.assertEqual(runner._supervisor_request(**kwargs)[1], {"total_tokens": 19})
            with self.assertRaises(RuntimeError) as raised:
                runner._supervisor_request(**kwargs)
        self.assertEqual(raised.exception.supervisor_usage, {})

    def test_retry_accounts_all_supervisor_attempts_including_failed_requests(self):
        for final_reply in ('{"decision":"approve"}', "not JSON"):
            with self.subTest(final_reply=final_reply):
                def supervise(text, options, index):
                    if index == 0:
                        raise RuntimeError("429 rate limit")
                    return final_reply

                models = _ConcurrencyModels(supervisor=supervise)
                _, task = self._submit(_StubApp(self.data_dir, models=models), self._images(1), workers=_workers(1),
                                       system_prompt="规则：" + final_reply)
                final = self._wait(task["id"])
                row = self.db.list_items(task["id"])[0]
                self.assertEqual(final["usage"]["total_tokens"], models.total_tokens())
                self.assertEqual(row["usage"]["total_tokens"], models.total_tokens())
                self.assertEqual(row["supervisor_usage"]["total_tokens"], 2003)

    def test_txt_tool_rounds_before_retry_or_auth_failure_remain_accounted(self):
        for error in ("429 rate limit", "401 unauthorized"):
            with self.subTest(error=error):
                def label(text, options, index):
                    if index < 2:
                        return self.tool_label(text, options, index)
                    if index == 2:
                        raise RuntimeError(error)
                    return '{"tags":["cat"],"description":"一只猫"}'
                models = _ConcurrencyModels(label=label)
                app = _StubApp(self.data_dir, models=models)
                _, task = self._submit(app, self._images(1), workers=_workers(1), write_txt=True,
                                       retry_limit=1, system_prompt="本次规则：" + error)
                final = self._wait(task["id"])
                row = self.db.list_items(task["id"])[0]
                self.assertEqual(final["usage"]["total_tokens"], models.total_tokens())
                self.assertEqual(row["usage"]["total_tokens"], models.total_tokens())
                self.assertEqual(final["status"], "done" if error.startswith("429") else "failed")
                if error.startswith("429"):
                    self.assertEqual(final["usage"]["total_tokens"], 1011)

    def test_cancel_while_acquiring_next_label_slot_cannot_borrow_last_image_usage(self):
        original = ProviderGate.acquire
        labels = {"count": 0}
        def cancel_second(gate, key, cancel_event=None):
            if str(key).startswith("online:worker"):
                labels["count"] += 1
                if labels["count"] == 2:
                    cancel_event.set()
            return original(gate, key, cancel_event)
        models = _ConcurrencyModels()
        with patch.object(ProviderGate, "acquire", cancel_second):
            _, task = self._submit(_StubApp(self.data_dir, models=models), self._images(2), workers=_workers(1))
            final = self._wait(task["id"])
        self.assertEqual(final["usage"]["total_tokens"], models.total_tokens())
        self.assertEqual(models.total_tokens(), 1002)
        cancelled = next(row for row in self.db.list_items(task["id"]) if row["state"] == "cancelled")
        self.assertEqual(cancelled["usage"], {})

    def test_cached_author_uses_original_model_and_slot_not_current_lane(self):
        barrier = threading.Barrier(2, timeout=5)

        def label(text, options, index):
            barrier.wait()
            return '{"tags":["cat"],"description":""}'

        models = _ConcurrencyModels(label=label)
        app = _StubApp(self.data_dir, models=models)
        images = self._images(2)
        _, task = self._submit(app, images)
        self.assertEqual(self._wait(task["id"])["status"], "done")
        original = next(row for row in self.db.list_items(task["id"]) if row["worker_slot"] == 1)
        image = next(path for path in images if path.name == original["name"])
        calls = dict(models.counters)
        _, repeated = self._submit(app, [image])
        self.assertEqual(self._wait(repeated["id"])["status"], "done")
        row = self.db.list_items(repeated["id"])[0]
        self.assertEqual(models.counters, calls)
        self.assertEqual(row["worker_slot"], 1)
        self.assertEqual(row["worker_model_name"], original["worker_model_name"])

    def test_duplicate_model_slots_do_not_invent_cached_author_slot(self):
        models = _ConcurrencyModels()
        app = _StubApp(self.data_dir, models=models)
        images = self._images(1)
        workers = _workers(1) * 2
        _, task = self._submit(app, images, workers=workers)
        self.assertEqual(self._wait(task["id"])["status"], "done")
        _, repeated = self._submit(app, images, workers=workers)
        self.assertEqual(self._wait(repeated["id"])["status"], "done")
        self.assertEqual(self.db.list_items(repeated["id"])[0]["worker_slot"], LABELER_UNKNOWN_WORKER_SLOT)

    def test_missing_approved_txt_is_not_reconstructed_from_caption_cache(self):
        models = _ConcurrencyModels()
        app = _StubApp(self.data_dir, models=models)
        images = self._images(1)
        _, task = self._submit(app, images, workers=_workers(1), write_txt=True)
        self.assertEqual(self._wait(task["id"])["status"], "done")
        with self.db._connect() as connection:
            connection.execute("UPDATE annotations SET approved_txt=NULL")
        calls = dict(models.counters)
        _, repeated = self._submit(app, images, workers=_workers(1), write_txt=True)
        self.assertEqual(self._wait(repeated["id"])["status"], "done")
        self.assertEqual(models.counters["label"], calls["label"] + 1)
        self.assertEqual(models.counters["supervisor"], calls["supervisor"] + 1)

    def test_old_database_gets_nullable_approved_txt_without_losing_rows(self):
        models = _ConcurrencyModels()
        _, task = self._submit(_StubApp(self.data_dir, models=models), self._images(1), workers=_workers(1))
        self.assertEqual(self._wait(task["id"])["status"], "done")
        original = self.db.annotations(task_id=task["id"])[0]
        with self.db._connect() as connection:
            connection.execute("ALTER TABLE annotations DROP COLUMN approved_txt")
        migrated = LabelerDB(self.data_dir)
        row = migrated.annotations(task_id=task["id"])[0]
        self.assertEqual(row["id"], original["id"])
        self.assertEqual(row["tags"], original["tags"])
        self.assertIsNone(row[LABELER_APPROVED_TXT_KEY])


class SkillReviewRulesTests(unittest.TestCase):
    def test_rule_drift_after_skill_run_retains_all_engine_usage(self):
        fixture = serial_fixtures.LabelerChatEngineTests("test_chat_engine_json_output_keeps_tags")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = fixture._chat_raw(concurrency_enabled=True,
            workers=[{"model_key": "online:stub", "model_name": "worker"}],
            supervisor={"model_key": "online:stub", "model_name": "judge"})
        path = fixture.images[0]
        def changed(engine, **kwargs):
            engine.last_usage = {"total_tokens": 34}
            engine.app.models.last_usage = {"total_tokens": 5}
            (fixture.skills_dir / "demo-label-skill" / "SKILL.md").write_text("规则已改变", encoding="utf-8")
            return {"tags": ["cat"], "description": "一只猫"}
        with patch.object(ChatSkillEngine, "run", changed):
            task = fixture.jobs.submit({"image_refs": [{"path": str(path), "name": path.name}], "config": config})
            final = fixture._wait(task["id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["usage"]["total_tokens"], 34)
        self.assertEqual(fixture.db.list_items(task["id"])[0]["usage"]["total_tokens"], 34)
        self.assertEqual(fixture.db.annotations(task_id=task["id"]), [])

    def test_actual_skill_and_extra_rules_reach_both_labeler_and_supervisor(self):
        fixture = serial_fixtures.LabelerChatEngineTests("test_chat_engine_json_output_keeps_tags")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def answer(profile, messages, options, status):
            if "你是图片标注审核员" in _message_text(messages):
                return '{"decision":"approve","reason":"ok"}'
            return '{"tags":["cat"],"description":"一只猫"}'

        fixture.app.models = _StubModels(answer)
        config = fixture._chat_raw(system_prompt="只描述主体", custom_prompt="忽略背景文字，仅保留主体体态",
                                   concurrency_enabled=True, workers=[{"model_key": "online:stub", "model_name": "worker"}],
                                   supervisor={"model_key": "online:stub", "model_name": "judge"})
        path = fixture.images[0]
        task = fixture.jobs.submit({"image_refs": [{"path": str(path), "name": path.name, "relative_path": "d/" + path.name}], "config": config})
        self.assertEqual(fixture._wait(task["id"])["status"], "done")
        calls = fixture.app.models.calls
        self.assertEqual(len(calls), 2)
        for call in calls:
            text = _message_text(call["messages"])
            self.assertIn("观察图片并输出", text)
            self.assertIn("忽略背景文字，仅保留主体体态", text)
            self.assertIn("只描述主体", text)
        self.assertIn("风格参考：简洁", _message_text(calls[-1]["messages"]))
        self.assertIsNone(calls[-1]["options"].get("tools"))

    def test_changed_skill_before_or_during_labeling_never_reaches_supervisor(self):
        for when in ("queued", "labeling"):
            with self.subTest(when=when):
                fixture = serial_fixtures.LabelerChatEngineTests("test_chat_engine_json_output_keeps_tags")
                fixture.setUp()
                try:
                    skill = fixture.skills_dir / "demo-label-skill" / "SKILL.md"
                    def answer(profile, messages, options, status):
                        skill.write_text("---\nname: demo-label-skill\ndescription: changed\n---\n全新规则", encoding="utf-8")
                        return '{"tags":["cat"],"description":"一只猫"}'
                    fixture.app.models = _StubModels(answer)
                    config = fixture._chat_raw(concurrency_enabled=True,
                        workers=[{"model_key": "online:stub", "model_name": "worker"}],
                        supervisor={"model_key": "online:stub", "model_name": "judge"})
                    path = fixture.images[0]
                    with patch.object(fixture.jobs.runner, "submit"):
                        task = fixture.jobs.submit({"image_refs": [{"path": str(path), "name": path.name}], "config": config})
                    if when == "queued":
                        answer(None, None, None, None)
                    fixture.jobs.runner._run_task(task["id"])
                    self.assertEqual(fixture.store.get_task(task["id"])["status"], "failed")
                    self.assertEqual(len(fixture.app.models.calls), 0 if when == "queued" else 1)
                    self.assertEqual(fixture.db.annotations(task_id=task["id"]), [])
                    self.assertIn("Skill 规则已改变", fixture.db.list_items(task["id"])[0]["error"])
                finally:
                    fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
