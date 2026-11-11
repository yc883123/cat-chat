# -*- coding: utf-8 -*-
"""异步 Job 韧性守门（重启恢复去重 / ComfyUI 丢失任务不再空转 / 子 Job 不锁对话）。

冻结的不变量（对应 2.2.0 之前的一次线上事故：任务面板几十个「运行中」停不掉、
且卡住的 Job 让对话发不出新消息）：

1. ``resume_interrupted()`` 幂等：恢复后必须给**源 Job** 打持久标记，否则每次重启都会
   把同一批中断任务再恢复一遍（1→2→4→8，实测放大成每对话 13 份）；
2. 相同 checkpoint 的独立 Job 各自恢复；只按持久源 Job / 接续 Job 关联防重；
3. ComfyUI 重启/清空队列后，既不在 ``/history`` 也不在 ``/queue`` 的 prompt 不再被当成
   「还在跑」空转到 ``wait_timeout``（默认 2 小时）：按预算重提交，用尽预算则记为丢失并收尾；
4. 后台子 Job（``parent_job_id`` 非空）不占用对话的活跃 Run 互斥位，否则一个卡死的
   异步 Job 会让该对话再也发不出新消息（HTTP 409，且前端只弹一句「已恢复其进度」）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.jobs import JobRegistry, JobSpec  # noqa: E402
from naiba.core.contracts import (  # noqa: E402
    JOB_RESUMED_FROM_KEY, JOB_RESUMED_INTO_KEY,
    JOB_SUBMISSION_GUARD_KEY, JOB_SUBMISSION_PENDING_KEY,
)
from naiba.storage.store import ChatStorage  # noqa: E402


def copy_checkpoint(checkpoint: dict) -> dict:
    """深拷贝 checkpoint（模拟真实链路里两条记录各存一份等价快照）。"""
    return json.loads(json.dumps(checkpoint, ensure_ascii=False))


class _ConfigStub:
    def __init__(self, data_dir: Path) -> None:
        self.data = {"imaging": {}}
        self._data_dir = data_dir

    def resolve_data_dir(self) -> Path:
        return self._data_dir


class _BusStub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._conditions: dict[str, threading.Condition] = {}

    def emit(self, job_id: str, payload: dict, raise_on_error: bool = True) -> None:
        self.events.append((job_id, payload))

    def ensure(self, job_id: str) -> threading.Condition:
        return self._conditions.setdefault(job_id, threading.Condition())


class _AppStub:
    """JobRegistry 只需要 storage / config / event_bus（不接 job_media_writer）。"""

    def __init__(self, storage: ChatStorage, config: _ConfigStub, bus: _BusStub) -> None:
        self.storage = storage
        self.config = config
        self.event_bus = bus


class _RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_job_resume_"))
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = ChatStorage(self.data_dir / "chat.db")
        self.config = _ConfigStub(self.data_dir)
        self.bus = _BusStub()
        self.registry = JobRegistry(_AppStub(self.storage, self.config, self.bus))
        self.conversation_id = str(self.storage.create_conversation("Job 韧性")["id"])

    def tearDown(self) -> None:
        self.registry.shutdown(timeout=2.0)

    def _make_job(
        self,
        kind: str = "comfyui",
        status: str = "interrupted",
        checkpoint: dict | None = None,
        params: dict | None = None,
        resumable: bool = True,
        parent_job_id: str = "parent-run",
        conversation_id: str | None = None,
    ) -> str:
        run = self.storage.create_run(
            conversation_id or self.conversation_id,
            "ComfyUI 批量生成",
            {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []},
            {
                "job_spec": {"kind": kind, "resumable": resumable},
                "params": params if params is not None else {"comfyui_url": "http://127.0.0.1:1", "workflows": [{"1": {}}]},
            },
            kind=kind,
            parent_job_id=parent_job_id,
            owner_session_id=conversation_id or self.conversation_id,
        )
        job_id = str(run["id"])
        self.storage.update_job(job_id, status=status, checkpoint=checkpoint or {})
        return job_id


class ResumeInterruptedDedupeTests(_RegistryCase):
    def test_equal_checkpoints_do_not_merge_independent_jobs(self) -> None:
        checkpoint = {"submitted": ["p1", "p2"], "completed": [{"index": 0, "prompt_id": "p1", "files": []}], "errors": []}
        # 先造旧的重复副本，再造较新的那条（list_background_tasks 按 created_at DESC）。
        duplicate = self._make_job(checkpoint=checkpoint)
        time.sleep(0.02)
        newest = self._make_job(checkpoint=copy_checkpoint(checkpoint))
        time.sleep(0.02)
        other_batch = self._make_job(checkpoint={"submitted": ["q1"], "completed": [], "errors": []})

        resumed = self.registry.resume_interrupted()

        self.assertEqual(len(resumed), 3, "checkpoint 相同也可能是用户独立提交的任务，不得吞掉")
        newest_row = self.storage.get_background_task(newest)
        duplicate_row = self.storage.get_background_task(duplicate)
        other_row = self.storage.get_background_task(other_batch)
        self.assertTrue(newest_row["result"].get("resumed_into"), "被恢复的源 Job 必须记录接续者")
        self.assertTrue(other_row["result"].get("resumed_into"), "不同批次的 Job 也各恢复一条")
        self.assertTrue(duplicate_row["result"].get("resumed_into"))
        self.assertFalse(duplicate_row["result"].get("resume_skipped"))

        active = [job for job in self.storage.list_background_tasks("", active_only=False) if job["kind"] == "comfyui"]
        self.assertEqual(len(active), 6, "3 条独立中断记录 + 3 条新恢复的 Job")

    def test_resume_interrupted_is_idempotent_across_restarts(self) -> None:
        checkpoint = {"submitted": ["p1"], "completed": [], "errors": []}
        self._make_job(checkpoint=checkpoint)
        time.sleep(0.02)
        self._make_job(checkpoint=copy_checkpoint(checkpoint))

        first = self.registry.resume_interrupted()
        second = self.registry.resume_interrupted()
        third = self.registry.resume_interrupted()

        self.assertEqual(len(first), 2)
        self.assertEqual(second, [], "同一批中断任务不得在下一轮启动里被反复复活")
        self.assertEqual(third, [])

    def test_non_resumable_job_is_left_alone(self) -> None:
        job_id = self._make_job(checkpoint={"submitted": ["p1"]}, resumable=False)
        self.assertEqual(self.registry.resume_interrupted(), [])
        row = self.storage.get_background_task(job_id)
        self.assertFalse(row["result"].get("resumed_into"), "未声明 resumable 的 Job 不得被自动恢复")


class _SimulatedProcessExit(BaseException):
    pass


class AtomicJobResumeTests(_RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.executed: list[str] = []
        self.registry._workers["check"] = self._complete

    def _complete(self, job_id, spec, cancel) -> None:
        source = self.storage.get_background_task(spec.params[JOB_RESUMED_FROM_KEY])
        self.assertEqual(source["result"][JOB_RESUMED_INTO_KEY], job_id)
        self.executed.append(job_id)
        self.storage.update_job(job_id, status="completed", finished=True)

    def _source(self, **kwargs) -> str:
        return self._make_job(kind="check", checkpoint={"handle": "shared-handle"}, **kwargs)

    def _restart(self) -> None:
        self.registry.shutdown(timeout=2)
        self.storage = ChatStorage(self.data_dir / "chat.db")
        self.registry = JobRegistry(_AppStub(self.storage, self.config, self.bus))
        self.registry._workers["check"] = self._complete

    def test_same_checkpoint_in_two_conversations_recovers_both(self) -> None:
        a = self._source()
        other = str(self.storage.create_conversation("另一条独立会话")["id"])
        b = self._source(conversation_id=other)
        ids = self.registry.resume_interrupted()
        self.registry.shutdown(timeout=2)
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(self.executed), 2)
        self.assertEqual({self.storage.get_background_task(i)["conversation_id"] for i in ids}, {self.conversation_id, other})
        for source in (a, b):
            self.assertIn(self.storage.get_background_task(source)["result"][JOB_RESUMED_INTO_KEY], ids)

    def test_concurrent_resume_returns_one_child_and_starts_one_worker(self) -> None:
        source = self._source()
        second = JobRegistry(_AppStub(self.storage, self.config, self.bus))
        second._workers["check"] = self._complete
        gate = threading.Barrier(2)
        returned, failures = [], []
        def resume(registry):
            try:
                gate.wait(timeout=2)
                returned.append(registry.resume(source, owner=self.conversation_id))
            except BaseException as exc:
                failures.append(exc)
        threads = [threading.Thread(target=resume, args=(r,)) for r in (self.registry, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.registry.shutdown(timeout=2)
        second.shutdown(timeout=2)
        self.assertEqual(failures, [])
        self.assertEqual(len(set(returned)), 1)
        self.assertEqual(self.executed, [returned[0]])
        self.assertEqual(len(self.storage.list_background_tasks(limit=200)), 2)

    def test_source_mark_failure_rolls_back_child_then_can_retry(self) -> None:
        source = self._source()
        with self.storage._connect() as db:
            db.execute("CREATE TRIGGER reject_resume_mark BEFORE UPDATE OF result ON background_tasks "
                       "WHEN json_extract(NEW.result, '$.resumed_into') IS NOT NULL "
                       "BEGIN SELECT RAISE(ABORT, 'resume mark failure'); END")
        with self.assertRaisesRegex(Exception, "resume mark failure"):
            self.registry.resume(source, owner=self.conversation_id)
        self.assertEqual(len(self.storage.list_background_tasks(limit=200)), 1)
        self.assertEqual(self.executed, [])
        with self.storage._connect() as db:
            db.execute("DROP TRIGGER reject_resume_mark")
        child = self.registry.resume(source, owner=self.conversation_id)
        self.registry.shutdown(timeout=2)
        self.assertEqual(self.executed, [child])

    def test_crash_after_registration_before_thread_start_recovers_child_once(self) -> None:
        source = self._source()
        with patch.object(self.registry, "_emit", side_effect=_SimulatedProcessExit):
            with self.assertRaises(_SimulatedProcessExit):
                self.registry.resume(source, owner=self.conversation_id)
        child = self.storage.get_background_task(source)["result"][JOB_RESUMED_INTO_KEY]
        self.assertEqual(self.storage.get_background_task(child)["checkpoint"], {"handle": "shared-handle"})
        self.assertEqual(self.executed, [])
        self._restart()
        resumed = self.registry.resume_interrupted()
        self.registry.shutdown(timeout=2)
        self.assertEqual(len(resumed), 1)
        self.assertEqual(self.executed, resumed)
        self.assertEqual(self.storage.get_run_snapshot(resumed[0])["params"][JOB_RESUMED_FROM_KEY], child)
        self.assertEqual(self.storage.get_background_task(source)["result"][JOB_RESUMED_INTO_KEY], child)
        self._restart()
        self.assertEqual(self.registry.resume_interrupted(), [])
        self.assertEqual(len(self.executed), 1)

    def test_legacy_child_without_source_mark_is_reconciled_without_execution(self) -> None:
        source = self._source()
        child = self._source(params={JOB_RESUMED_FROM_KEY: source})
        self.storage.update_job(child, status="completed", finished=True)
        self.assertEqual(self.registry.resume(source, owner=self.conversation_id), child)
        self.assertEqual(self.executed, [])
        self.assertEqual(self.storage.get_background_task(source)["result"][JOB_RESUMED_INTO_KEY], child)

    def test_ambiguous_legacy_siblings_are_not_replayed(self) -> None:
        source = self._source()
        self._source(params={JOB_RESUMED_FROM_KEY: source})
        self._source(params={JOB_RESUMED_FROM_KEY: source})
        with self.assertLogs("naiba.jobs", level="ERROR"):
            self.assertEqual(self.registry.resume_interrupted(), [])
        self.assertEqual(self.executed, [])
        self.assertEqual(len(self.storage.list_background_tasks(limit=200)), 3)

    def test_missing_recorded_child_is_not_recreated(self) -> None:
        source = self._source()
        self.storage.update_job(source, result={JOB_RESUMED_INTO_KEY: "missing-child"})
        with self.assertRaisesRegex(ValueError, "缺失"):
            self.registry.resume(source, owner=self.conversation_id)
        self.assertEqual(self.executed, [])
        self.assertEqual(len(self.storage.list_background_tasks(limit=200)), 1)

    def test_same_checkpoint_with_different_kind_is_not_merged(self) -> None:
        self._source()
        self.registry._workers["http_poll"] = self._complete
        self._make_job(kind="http_poll", checkpoint={"handle": "shared-handle"})
        self.assertEqual(len(self.registry.resume_interrupted()), 2)
        self.registry.shutdown(timeout=2)
        self.assertEqual(len(self.executed), 2)


class ResumeLabelTests(_RegistryCase):
    """恢复/重试产生的新 Job 不得把 ``current_step`` 当任务名。

    事故（2026-09-20 截图）：``resume()`` 用 ``job["current_step"]`` 当新 Job 的 label，
    而 current_step 是**进度文字**（「完成 8/10」「提交第 3/10 段」）。label 会落进
    ``background_tasks.message``，而它直接顶在任务面板的标题位——于是同一组里并排两条行
    变成「ComfyUI 批量生成」与「完成 8/10」，用户读到的是一句没有主语、也看不出是恢复件的进度。
    修复口径：任务名沿用源 Job 的原名并加「（恢复）」后缀，进度继续归 current_step。
    """

    def test_resume_keeps_original_label_instead_of_progress_text(self) -> None:
        job_id = self._make_job(
            status="interrupted",
            checkpoint={"submitted": ["p1"], "completed": [], "errors": []},
        )
        self.storage.update_job(job_id, current_step="完成 8/10")  # 重启那一刻的进度文字
        new_id = self.registry.resume(job_id, owner=self.conversation_id)

        self.assertTrue(new_id, "可恢复的 Job 应当起一条新 Job")
        row = self.storage.get_background_task(str(new_id)) or {}
        self.assertEqual(row["message"], "ComfyUI 批量生成（恢复）", "标题必须是原名，不是进度")
        self.assertNotIn("完成 8/10", str(row["message"]))
        # 源 Job 自身保持原样：进度文字留在 current_step 里（面板的「当前步骤」还在展示它）
        source = self.storage.get_background_task(job_id) or {}
        self.assertEqual(source["current_step"], "完成 8/10")

    def test_resume_without_original_label_falls_back_to_kind_name(self) -> None:
        """源 Job 没有可读名字时**留空**，而不是退化成 current_step。

        留空时 ``create_run`` 会写成 ``Job(kind)``，前端 ``taskDisplayTitle`` 认得这个兜底名
        并改用类型名（「ComfyUI 生成」）显示。若这里退化成进度文字，前端就无从判断了。
        """
        run = self.storage.create_run(
            self.conversation_id,
            "",  # 无 label 的 Job：message 为空
            {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []},
            {
                "job_spec": {"kind": "comfyui", "resumable": True},
                "params": {"comfyui_url": "http://127.0.0.1:1", "workflows": [{"1": {}}]},
            },
            kind="comfyui",
            parent_job_id="parent-run",
            owner_session_id=self.conversation_id,
        )
        job_id = str(run["id"])
        self.storage.update_job(
            job_id, status="interrupted", checkpoint={"submitted": ["p1"]}, current_step="完成 8/10"
        )
        new_id = self.registry.resume(job_id, owner=self.conversation_id)

        row = self.storage.get_background_task(str(new_id)) or {}
        self.assertEqual(row["message"], "Job(comfyui)", "无名时留空，交给前后端的类型名兜底")

    def test_repeated_resume_does_not_stack_suffixes(self) -> None:
        """连续恢复（恢复件又被恢复）不得叠成「X（恢复）（恢复）」。"""
        first = self._make_job(status="interrupted", checkpoint={"submitted": ["p1"]})
        second = self.registry.resume(first, owner=self.conversation_id)
        self.storage.update_job(str(second), status="interrupted")
        third = self.registry.resume(str(second), owner=self.conversation_id)

        row = self.storage.get_background_task(str(third)) or {}
        self.assertEqual(row["message"], "ComfyUI 批量生成（恢复）")


class _ComfyStubState:
    def __init__(self) -> None:
        self.history: dict[str, dict] = {}
        self.queue: list[str] = []
        self.submits: list[str] = []
        self.next_prompt = 0


class _ComfyStubHandler(BaseHTTPRequestHandler):
    state: _ComfyStubState

    def log_message(self, *args) -> None:  # noqa: D102 - 静音测试期访问日志
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        state = type(self).state
        if self.path.startswith("/history/"):
            prompt_id = self.path.rsplit("/", 1)[-1]
            entry = state.history.get(prompt_id)
            self._send({prompt_id: entry} if entry else {})
            return
        if self.path == "/queue":
            self._send({
                "queue_running": [],
                "queue_pending": [[0, prompt_id, {}, {}, {}] for prompt_id in state.queue],
            })
            return
        self._send({"system": {}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        state = type(self).state
        state.next_prompt += 1
        prompt_id = f"p{state.next_prompt}"
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        state.submits.append(prompt_id)
        self._send({"prompt_id": prompt_id})


class CheckSubmissionRecoveryTests(_RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.requests = {"submit": 0, "poll": 0}
        counts = self.requests
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def _respond(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                counts["submit"] += 1
                self._respond({"id": "accepted-once"})
            def do_GET(self):
                counts["poll"] += 1
                self._respond({"status": "completed"})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.http_thread.start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.params = {"submit": {"url": base + "/submit", "json": {}},
                       "poll": {"url": base + "/poll"},
                       "check": {"interval_seconds": 0, "timeout_seconds": 2, "max_attempts": 2}}

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.http_thread.join(2)
        super().tearDown()

    def _restart(self):
        self.registry.shutdown(timeout=2)
        self.storage = ChatStorage(self.data_dir / "chat.db")
        self.registry = JobRegistry(_AppStub(self.storage, self.config, self.bus))

    def _run(self, source):
        self.registry._run_check(source, JobSpec(kind="check", conversation_id=self.conversation_id,
                                                params=self.params, resumable=True), threading.Event())

    def test_restart_with_saved_handle_only_polls_original_request(self) -> None:
        source = self._make_job(kind="check", status="running", params=self.params)
        with patch.object(self.registry, "_poll_status", side_effect=_SimulatedProcessExit):
            with self.assertRaises(_SimulatedProcessExit):
                self._run(source)
        self.assertEqual(self.requests["submit"], 1)
        self.assertEqual(self.storage.get_background_task(source)["checkpoint"]["handle"], {"id": "accepted-once"})
        self._restart()
        children = self.registry.resume_interrupted()
        self.assertEqual(len(children), 1)
        self.registry._threads[children[0]].join(2)
        self.assertFalse(self.registry._threads[children[0]].is_alive())
        self.assertEqual(self.storage.get_background_task(children[0])["status"], "completed")
        self.assertEqual(self.requests, {"submit": 1, "poll": 1})
        self._restart()
        self.assertEqual(self.registry.resume_interrupted(), [])
        self.assertEqual(self.requests["submit"], 1)

    def test_remote_acceptance_without_saved_handle_never_resubmits(self) -> None:
        source = self._make_job(kind="check", status="running", params=self.params)
        with patch.object(self.registry, "_save_job_submission", side_effect=_SimulatedProcessExit):
            with self.assertRaises(_SimulatedProcessExit):
                self._run(source)
        self.assertEqual(self.requests["submit"], 1)
        self.assertIn(JOB_SUBMISSION_PENDING_KEY, self.storage.get_background_task(source)["checkpoint"])
        self._restart()
        self.assertEqual(self.registry.resume_interrupted(), [])
        with self.assertRaisesRegex(ValueError, "结果未知"):
            self.registry.resume(source, owner=self.conversation_id)
        self.assertEqual(self.requests["submit"], 1)

    def test_legacy_checkpoint_without_handle_does_not_assume_unsubmitted(self) -> None:
        source = self._make_job(kind="check", checkpoint={"attempt": 1}, params=self.params)
        child = self.registry.resume(source, owner=self.conversation_id)
        self.registry._threads[child].join(2)
        self.assertFalse(self.registry._threads[child].is_alive())
        self.assertEqual(self.requests["submit"], 0)
        self.assertEqual(self.storage.get_background_task(child)["status"], "failed")
        self.assertIn("结果未知", self.storage.get_background_task(child)["error"])


class ComfySubmissionRecoveryTests(_RegistryCase):
    def _restart(self):
        self.registry.shutdown(timeout=2)
        self.storage = ChatStorage(self.data_dir / "chat.db")
        self.registry = JobRegistry(_AppStub(self.storage, self.config, self.bus))

    def test_legacy_saved_prompt_is_polled_without_resubmission(self) -> None:
        source = self._make_job(status="running")
        spec = JobSpec(kind="comfyui", conversation_id=self.conversation_id,
                       params={"workflow": {"1": {}}, JOB_RESUMED_FROM_KEY: "legacy-source"},
                       checkpoint={"prompt_ids": ["known-prompt"], "completed_shots": []})
        with patch.object(self.registry, "_comfyui_reachable", return_value=True), \
             patch.object(self.registry, "_comfyui_submit") as submit, \
             patch.object(self.registry, "_comfyui_wait_history", return_value=(True, ["file.png"], "")) as poll:
            self.registry._run_comfyui(source, spec, threading.Event())
        submit.assert_not_called()
        self.assertEqual(poll.call_args.args[1], "known-prompt")
        self.assertEqual(self.storage.get_background_task(source)["status"], "completed")

    def test_batch_acceptance_before_checkpoint_is_not_replayed_after_restart(self) -> None:
        source = self._make_job(status="running")
        workflows = [{"1": {}}]
        spec = JobSpec(kind="comfyui", conversation_id=self.conversation_id, params={"workflows": workflows})
        with patch.object(self.registry, "_comfyui_submit", return_value=("accepted-prompt", "")) as submit, \
             patch.object(self.registry, "_save_job_submission", side_effect=_SimulatedProcessExit):
            with self.assertRaises(_SimulatedProcessExit):
                self.registry._run_comfyui_batch(source, spec, threading.Event(), "http://unused", workflows)
        submit.assert_called_once()
        self.assertEqual(self.storage.get_background_task(source)["checkpoint"]["submitted"], [])
        self._restart()
        self.assertEqual(self.registry.resume_interrupted(), [])
        self.assertIn("结果未知", self.storage.get_background_task(source)["error"])

    def test_batch_resubmit_acceptance_before_checkpoint_is_not_replayed(self) -> None:
        source = self._make_job(status="running")
        workflows = [{"1": {}}]
        checkpoint = {"submitted": ["lost-prompt"], "completed": [], "errors": [], JOB_SUBMISSION_GUARD_KEY: 1}
        spec = JobSpec(kind="comfyui", conversation_id=self.conversation_id,
                       params={"workflows": workflows}, checkpoint=checkpoint)
        with patch.object(self.registry, "_comfyui_queue_ids", return_value=set()), \
             patch.object(self.registry, "_comfyui_history_once", return_value=(False, [], "")), \
             patch.object(self.registry, "_comfyui_submit", return_value=("accepted-replacement", "")) as submit, \
             patch.object(self.registry, "_save_job_submission", side_effect=_SimulatedProcessExit):
            with self.assertRaises(_SimulatedProcessExit):
                self.registry._run_comfyui_batch(source, spec, threading.Event(), "http://unused", workflows)
        submit.assert_called_once()
        self._restart()
        self.assertEqual(self.registry.resume_interrupted(), [])
        self.assertEqual(self.storage.get_background_task(source)["checkpoint"]["submitted"], ["lost-prompt"])

    def test_failed_history_query_does_not_authorize_resubmission(self) -> None:
        source = self._make_job(status="running")
        workflows = [{"1": {}}]
        checkpoint = {"submitted": ["known-prompt"], JOB_SUBMISSION_GUARD_KEY: 1}
        spec = JobSpec(kind="comfyui", conversation_id=self.conversation_id,
                       params={"workflows": workflows}, checkpoint=checkpoint)
        cancel = threading.Event()
        def history(*args):
            cancel.set()
            return False, [], "UNKNOWN:history request failed"
        with patch.object(self.registry, "_comfyui_queue_ids", return_value=set()), \
             patch.object(self.registry, "_comfyui_history_once", side_effect=history), \
             patch.object(self.registry, "_comfyui_submit") as submit, \
             patch("naiba.jobs.time.sleep"):
            self.registry._run_comfyui_batch(source, spec, cancel, "http://unused", workflows)
        submit.assert_not_called()


class ComfyBatchLostPromptTests(_RegistryCase):
    """ComfyUI 重启后 prompt 既不在历史也不在队列：必须收尾，不能空转 2 小时。"""

    def setUp(self) -> None:
        super().setUp()
        self.state = _ComfyStubState()
        handler = type("_Handler", (_ComfyStubHandler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _run_batch(self, job_id: str, max_resubmit: int) -> dict:
        spec = JobSpec(
            kind="comfyui",
            conversation_id=self.conversation_id,
            params={
                "comfyui_url": self.base,
                "workflows": [{"1": {"class_type": "Demo"}}],
                "wait_timeout": 600,
                "max_resubmit": max_resubmit,
            },
            label="ComfyUI 批量生成",
            parent_job_id="parent-run",
            owner_session_id=self.conversation_id,
        )
        self.registry._run_comfyui_batch(  # noqa: SLF001 - 直接驱动 Worker，断言终态
            job_id, spec, threading.Event(), self.base, spec.params["workflows"]
        )
        return self.storage.get_background_task(job_id) or {}

    def test_lost_prompt_finishes_instead_of_polling_forever(self) -> None:
        job_id = self._make_job(status="running")
        row = self._run_batch(job_id, max_resubmit=0)

        self.assertIn(row["status"], {"failed", "cancelled"})
        self.assertNotIn(row["status"], {"running", "queued"}, "丢失的 prompt 不得让 Job 永远停在运行中")
        errors = row["result"].get("errors") or []
        self.assertTrue(errors, "丢失必须落成可解释的 errors")
        self.assertIn("丢失", str(errors[0].get("error") or ""))
        self.assertEqual(len(self.state.submits), 1, "无重提交预算时只提交最初一次")

    def test_lost_prompt_is_resubmitted_within_budget(self) -> None:
        job_id = self._make_job(status="running")
        row = self._run_batch(job_id, max_resubmit=1)

        self.assertEqual(len(self.state.submits), 2, "预算内应重提交一次")
        self.assertNotIn(row["status"], {"running", "queued"})
        checkpoint = row["checkpoint"]
        self.assertEqual(checkpoint.get("submitted"), self.state.submits[-1:], "checkpoint 必须换成新 prompt_id")

    def test_finished_prompt_completes(self) -> None:
        prompt_id = "p1"
        self.state.queue = [prompt_id]
        self.state.history[prompt_id] = {
            "status": {"status_str": "success"},
            "outputs": {"9": {"images": [{"filename": "shot.mp4", "subfolder": "", "type": "output"}]}},
        }
        job_id = self._make_job(status="running")
        row = self._run_batch(job_id, max_resubmit=0)

        self.assertEqual(row["status"], "completed")
        completed = row["result"].get("completed") or []
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(self.state.submits), 1, "产物已就绪时不得重复提交")


class _ComfyRejectHandler(_ComfyStubHandler):
    """POST /prompt 一律 400 + node_errors（复刻 rgthree seed 超上限被拒的响应）。"""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._send({
            "error": {
                "type": "prompt_outputs_failed_validation",
                "message": "Prompt outputs failed validation",
                "details": "",
                "extra_info": {},
            },
            "node_errors": {
                "20": {
                    "errors": [{
                        "type": "value_bigger_than_max",
                        "message": "Value 6906715779295766020 bigger than max of 1125899906842624",
                        "details": "seed",
                        "extra_info": {},
                    }],
                    "dependent_outputs": ["191"],
                    "class_type": "Seed (rgthree)",
                },
            },
        }, status=400)


class ComfyBatchSubmitRejectTests(_RegistryCase):
    """ComfyUI 拒收（HTTP 400）时，原因必须写进任务 error 与 result.errors。

    2026-09-20 事故：rgthree seed 超上限被 400 拒收，但 _comfyui_submit 只回 None，
    任务上只剩「第 1 段提交失败」——模型看不到原因连盲试 6 次，最后绕道 MCP 才成功。
    """

    def setUp(self) -> None:
        super().setUp()
        handler = type("_RejectHandler", (_ComfyRejectHandler,), {})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_rejected_submit_surfaces_comfyui_reason(self) -> None:
        job_id = self._make_job(status="running")
        spec = JobSpec(
            kind="comfyui",
            conversation_id=self.conversation_id,
            params={
                "comfyui_url": self.base,
                "workflows": [{"20": {"class_type": "Seed (rgthree)", "inputs": {"seed": -1}}}],
                "wait_timeout": 600,
            },
            label="ComfyUI 批量生成",
            parent_job_id="parent-run",
            owner_session_id=self.conversation_id,
        )
        self.registry._run_comfyui_batch(  # noqa: SLF001 - 直接驱动 Worker，断言终态
            job_id, spec, threading.Event(), self.base, spec.params["workflows"]
        )
        row = self.storage.get_background_task(job_id) or {}
        self.assertEqual(row["status"], "failed")
        self.assertIn("Seed (rgthree)", row["error"], "任务错误必须带节点类型，模型才能自诊")
        self.assertIn("bigger than max", row["error"])
        self.assertNotIn(JOB_SUBMISSION_PENDING_KEY, row["checkpoint"], "明确的校验拒绝仍允许人工重试")
        errors = (row["result"] or {}).get("errors") or []
        self.assertTrue(errors, "逐段错误必须落进 result.errors")
        self.assertEqual(errors[0].get("index"), 0)
        self.assertIn("Seed (rgthree)", str(errors[0].get("error") or ""))


class ChildJobDoesNotLockConversationTests(unittest.TestCase):
    """运行中的后台子 Job 不得占用对话互斥位（否则对话发不出新消息）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_job_lock_"))
        self.storage = ChatStorage(self.tmp / "chat.db")
        self.conversation_id = str(self.storage.create_conversation("对话锁")["id"])
        self.agent = {"id": "general", "name": "通用 Agent"}
        self.snapshot = {"model_key": "online:demo", "provider_id": "demo"}

    def test_running_child_job_does_not_block_new_turn(self) -> None:
        run = self.storage.create_chat_run(
            self.conversation_id, "发起批量生成", [], self.agent, self.snapshot, "craft"
        )
        run_id = str(run["id"])
        child = self.storage.create_run(
            self.conversation_id,
            "ComfyUI 批量生成",
            self.agent,
            {"job_spec": {"kind": "comfyui", "resumable": True}},
            kind="comfyui",
            parent_job_id=run_id,
            owner_session_id=self.conversation_id,
        )
        # 父 Run 已收尾，只剩后台子 Job 在长跑（事故里的形态）
        self.storage.update_job(run_id, status="completed", finished=True)
        active = self.storage.list_background_tasks(self.conversation_id, active_only=True)
        self.assertEqual([job["kind"] for job in active], ["comfyui"])
        self.assertTrue(child["id"])

        self.assertIsNone(
            self.storage.active_run(self.conversation_id),
            "后台子 Job 不应被当成活跃 Run 占用对话互斥位",
        )
        again = self.storage.create_chat_run(
            self.conversation_id, "继续下一轮", [], self.agent, self.snapshot, "craft"
        )
        self.assertTrue(str(again["id"]), "子 Job 在跑时仍必须能发起新的一轮")

    def test_active_top_level_run_still_blocks(self) -> None:
        self.storage.create_chat_run(
            self.conversation_id, "第一轮", [], self.agent, self.snapshot, "craft"
        )
        self.assertIsNotNone(self.storage.active_run(self.conversation_id))
        with self.assertRaisesRegex(RuntimeError, "ACTIVE_RUN"):
            self.storage.create_chat_run(
                self.conversation_id, "第二轮", [], self.agent, self.snapshot, "craft"
            )


if __name__ == "__main__":
    unittest.main()
