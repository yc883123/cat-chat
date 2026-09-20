# -*- coding: utf-8 -*-
"""异步 Job 韧性守门（重启恢复去重 / ComfyUI 丢失任务不再空转 / 子 Job 不锁对话）。

冻结的不变量（对应 2.2.0 之前的一次线上事故：任务面板几十个「运行中」停不掉、
且卡住的 Job 让对话发不出新消息）：

1. ``resume_interrupted()`` 幂等：恢复后必须给**源 Job** 打持久标记，否则每次重启都会
   把同一批中断任务再恢复一遍（1→2→4→8，实测放大成每对话 13 份）；
2. checkpoint 完全相同的中断 Job 视为同一批的重复副本，只恢复最新的一条；
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.jobs import JobRegistry, JobSpec  # noqa: E402
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
    ) -> str:
        run = self.storage.create_run(
            self.conversation_id,
            "ComfyUI 批量生成",
            {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []},
            {
                "job_spec": {"kind": kind, "resumable": resumable},
                "params": params if params is not None else {"comfyui_url": "http://127.0.0.1:1", "workflows": [{"1": {}}]},
            },
            kind=kind,
            parent_job_id=parent_job_id,
            owner_session_id=self.conversation_id,
        )
        job_id = str(run["id"])
        self.storage.update_job(job_id, status=status, checkpoint=checkpoint or {})
        return job_id


class ResumeInterruptedDedupeTests(_RegistryCase):
    def test_duplicate_interrupted_jobs_are_resumed_once(self) -> None:
        checkpoint = {"submitted": ["p1", "p2"], "completed": [{"index": 0, "prompt_id": "p1", "files": []}], "errors": []}
        # 先造旧的重复副本，再造较新的那条（list_background_tasks 按 created_at DESC）。
        duplicate = self._make_job(checkpoint=checkpoint)
        time.sleep(0.02)
        newest = self._make_job(checkpoint=copy_checkpoint(checkpoint))
        time.sleep(0.02)
        other_batch = self._make_job(checkpoint={"submitted": ["q1"], "completed": [], "errors": []})

        resumed = self.registry.resume_interrupted()

        self.assertEqual(len(resumed), 2, "应按 checkpoint 指纹各恢复一条，而不是每条中断记录都恢复")
        newest_row = self.storage.get_background_task(newest)
        duplicate_row = self.storage.get_background_task(duplicate)
        other_row = self.storage.get_background_task(other_batch)
        self.assertTrue(newest_row["result"].get("resumed_into"), "被恢复的源 Job 必须记录接续者")
        self.assertTrue(other_row["result"].get("resumed_into"), "不同批次的 Job 也各恢复一条")
        self.assertFalse(duplicate_row["result"].get("resumed_into"))
        self.assertTrue(duplicate_row["result"].get("resume_skipped"), "重复副本必须打持久标记，否则下一轮又会被复活")

        active = [job for job in self.storage.list_background_tasks("", active_only=False) if job["kind"] == "comfyui"]
        self.assertEqual(len(active), 5, "3 条中断记录 + 2 条新恢复的 Job")

    def test_resume_interrupted_is_idempotent_across_restarts(self) -> None:
        checkpoint = {"submitted": ["p1"], "completed": [], "errors": []}
        self._make_job(checkpoint=checkpoint)
        time.sleep(0.02)
        self._make_job(checkpoint=copy_checkpoint(checkpoint))

        first = self.registry.resume_interrupted()
        second = self.registry.resume_interrupted()
        third = self.registry.resume_interrupted()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "同一批中断任务不得在下一轮启动里被反复复活")
        self.assertEqual(third, [])

    def test_non_resumable_job_is_left_alone(self) -> None:
        job_id = self._make_job(checkpoint={"submitted": ["p1"]}, resumable=False)
        self.assertEqual(self.registry.resume_interrupted(), [])
        row = self.storage.get_background_task(job_id)
        self.assertFalse(row["result"].get("resumed_into"), "未声明 resumable 的 Job 不得被自动恢复")


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
        run, _ = self.storage.create_chat_run(
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
        again, _ = self.storage.create_chat_run(
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
