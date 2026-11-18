# -*- coding: utf-8 -*-
"""造梦机对话域「§8 全量验收口径」的自动化守门层（证据层 L1：引擎接线 + 本地替身）。

与 ``test_comfyui_dream.py`` 的分工：那边验服务/契约的形状，这里验**真实
ToolExecutor 权限确认状态机**接上真实 ``dream_*`` 定义与 handler 之后的行为，
以及竞态 / 重启 / 越会话 / 结果晚到这些「必须有对应守门」的 §8 条目。

边界（不冒充真机）：
- 数据目录是临时目录，ComfyUI 远端与 runner 全部打桩，不联网、不启动真实生成；
- 真机样本（L3）、浏览器回归（L2）、教程同源（L2）见
  ``docs/plans/2026-11-18-造梦机对话域全量验收口径.md``，本文件不给它们背书。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.comfyui import client  # noqa: E402
from naiba.comfyui.batches import ComfyUIBatchManager  # noqa: E402
from naiba.comfyui.dream import DreamService, DreamSnapshotStore  # noqa: E402
from naiba.comfyui.jobs import ComfyUIJobManager  # noqa: E402
from naiba.comfyui.runner import ComfyUIRunner  # noqa: E402
from naiba.comfyui.store import ComfyUIStore  # noqa: E402
from naiba.mcp import MCPRegistry  # noqa: E402
from naiba.run.job_waits import parse_wait_target  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402
from naiba.tools.executor import ToolExecutor  # noqa: E402
from naiba.tools.providers import dream as dream_provider  # noqa: E402

WORKFLOW_ID = "cw-dream0001"
ALL_TOOLS = [
    "dream_workflows", "dream_workflow_details", "dream_prepare", "dream_submit",
    "dream_task_status", "dream_task_cancel", "dream_task_wait",
]


class _WaitCoordinator:
    """记录宿主的完成通知登记；reuse 由 note 模拟，不启动真实协调器线程。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.note = ""

    def register(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": f"w{len(self.calls)}"}, self.note


class AcceptanceHarness(unittest.TestCase):
    """临时造梦机库 + 真实 ToolExecutor 接线（def/alias 解析器与生产同构）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.waits = _WaitCoordinator()
        self.app = SimpleNamespace(
            paths=SimpleNamespace(data_dir=self.root, public_dir=self.root),
            config=SimpleNamespace(data={"comfyui_url": "http://127.0.0.1:8188"}),
            storage=SimpleNamespace(get_run_snapshot=lambda run_id: {}),
            job_waits=self.waits,
        )
        self.store = ComfyUIStore(self.root)
        self.runner = ComfyUIRunner(self.app, self.store)
        self.jobs = ComfyUIJobManager(self.app, self.store, self.runner)
        self.batches = ComfyUIBatchManager(self.app, self.store, self.jobs)
        self.app.comfyui = SimpleNamespace(store=self.store, jobs=self.jobs, batches=self.batches)
        self.service = DreamService(self.app)
        self.app.comfyui.dream = self.service
        self.prompt = {
            "5": {"class_type": "FakePrompt", "inputs": {"text": "hello", "seed": -1}},
            "6": {"class_type": "EmptyLatentImage", "inputs": {"width": 832, "height": 1216}},
        }
        self.params = [
            {"node_id": "5", "field_name": "text", "field_type": "text",
             "control": "multiline", "role": "positive_prompt", "label": "正文"},
            {"node_id": "5", "field_name": "seed", "field_type": "seed", "label": "种子"},
            {"node_id": "6", "field_name": "width", "field_type": "number",
             "default": 832, "role": "resolution", "label": "宽"},
            {"node_id": "6", "field_name": "height", "field_type": "number",
             "default": 1216, "role": "resolution", "label": "高"},
        ]
        self.store.save_workflow({"id": WORKFLOW_ID, "name": "对话验收流",
                                  "prompt": self.prompt, "params": self.params})
        self.service.chat_configs.save(
            WORKFLOW_ID, self.store.payload_hash(self.prompt),
            [
                {"id": "b1", "purpose": "positive_prompt", "display_name": "主体提示词",
                 "description": "", "slots": [{"node_id": "5", "field_name": "text", "name": ""}],
                 "ask": "always", "per_item": False},
            ],
            workflow_name="对话验收流",
        )
        self.ownership = {"kind": "conversation", "conversation_id": "conv-1",
                          "owner_id": "conv-1", "origin_run_id": "run-1"}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(client._DIRECT_OPENER, "open",
                          side_effect=AssertionError("network forbidden")).start()
        mock.patch.object(client, "reachable", return_value=None).start()
        mock.patch.object(self.runner, "submit", return_value=None).start()
        mock.patch("naiba.subagent.job_wait_execution_snapshot", return_value={}).start()

    def ctx(self, **extra):
        context = {
            "conv_kind": "main", "run_origin": "user", "depth": 0,
            "conversation_id": "conv-1", "owner_session_id": "conv-1",
            "run_id": "run-1", "allowed_tools": list(ALL_TOOLS),
        }
        context.update(extra)
        return context

    def executor(self, mode: str) -> ToolExecutor:
        """真实权限状态机 + 真实 Provider 绑定（含 B9b 确认卡摘要 policy）。"""
        specs = {spec.name: spec for spec in dream_provider.DreamToolProvider(self.app).tools()}
        engine = ToolExecutor(self.root, sys.executable, 60, MCPRegistry([]),
                              permission_mode=mode)
        engine.set_def_resolver(specs.get)
        engine.set_alias_resolver(lambda name: name)
        return engine

    def prepare_single(self) -> str:
        record = self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="single",
            values={"b1": "一只猫"},
        )
        return str(record["id"])

    def prepare_batch(self) -> str:
        """两条条目的批量预检（positive_prompt 由逐条 items 回答）。"""
        record = self.service.prepare(
            ownership=self.ownership, workflow_id=WORKFLOW_ID, mode="batch",
            values={},
            items=[
                {"name": "甲", "values": {"b1": "场景一"}, "shots": 1},
                {"name": "乙", "values": {"b1": "场景二"}, "shots": 2},
            ],
        )
        return str(record["id"])

    def task_count(self) -> int:
        return len(self.store.list_tasks())

    def batch_count(self) -> int:
        return len(self.batches.list())

    @staticmethod
    def confirm_id(text: str) -> str:
        return text.split(":", 2)[1]


class PermissionChainTests(AcceptanceHarness):
    """§7.8 三档权限表：真实 ToolExecutor 确认链（此前只有 policy 形状守门）。"""

    def test_readonly_tools_never_ask_in_any_mode(self):
        cases = [
            ("dream_workflows", {}),
            ("dream_workflow_details", {"workflow_id": WORKFLOW_ID}),
            ("dream_prepare", {"workflow_id": WORKFLOW_ID, "values": {"b1": "一只猫"}}),
            ("dream_task_status", {"target_ids": ["comfy-task:cf-none"]}),
            ("dream_task_wait", {"target_ids": ["comfy-task:cf-none"]}),
        ]
        for mode in ("confirm", "auto", "full"):
            engine = self.executor(mode)
            for tool, args in cases:
                with self.subTest(mode=mode, tool=tool):
                    _ok, out = engine.execute(tool, dict(args), [], self.ctx())
                    self.assertFalse(out.startswith("NEED_CONFIRM:"),
                                     f"{mode}/{tool} 是只读/预检设施，不得弹动作确认：{out}")
                    self.assertEqual(engine.pending_confirmation, {})

    def test_submit_confirm_asks_once_then_creates_exactly_one_task(self):
        snapshot_id = self.prepare_single()
        engine = self.executor("confirm")
        ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], self.ctx())
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        self.assertEqual(len(engine.pending_confirmation), 1)
        self.assertEqual(self.task_count(), 0, "确认卡未批准前绝不创建生成任务")
        ok2, out2 = engine.confirm_execute(self.confirm_id(out))
        self.assertTrue(ok2, out2)
        self.assertEqual(self.task_count(), 1)
        payload = json.loads(out2)
        self.assertTrue(payload["notify_registered"], "提交成功后宿主必须登记通知")
        ok3, out3 = engine.confirm_execute(self.confirm_id(out))
        self.assertFalse(ok3, "同一张确认卡只能消费一次")
        self.assertIn("无效或已过期", out3)
        self.assertEqual(self.task_count(), 1, "重复批准不得生成第二次")

    def test_submit_confirm_reject_creates_nothing(self):
        snapshot_id = self.prepare_single()
        engine = self.executor("confirm")
        _ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], self.ctx())
        ok2, out2 = engine.reject_execute(self.confirm_id(out))
        self.assertFalse(ok2)
        self.assertIn("拒绝执行", out2)
        self.assertEqual(self.task_count(), 0)
        self.assertEqual(self.waits.calls, [], "未提交就不该登记完成通知")

    def test_submit_auto_and_full_execute_without_confirmation(self):
        for mode in ("auto", "full"):
            with self.subTest(mode=mode):
                snapshot_id = self.prepare_single()
                engine = self.executor(mode)
                ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id},
                                         [], self.ctx())
                self.assertTrue(ok, out)
                self.assertFalse(out.startswith("NEED_CONFIRM:"), out)
                self.assertEqual(engine.pending_confirmation, {})

    def test_cancel_confirm_asks_auto_and_full_do_not(self):
        snapshot_id = self.prepare_single()
        engine = self.executor("auto")
        ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], self.ctx())
        self.assertTrue(ok, out)
        target = json.loads(out)["task_id"]
        for mode, expect_confirm in (("confirm", True), ("auto", False), ("full", False)):
            with self.subTest(mode=mode):
                engine = self.executor(mode)
                _ok, text = engine.execute("dream_task_cancel", {"target_id": target},
                                           [], self.ctx())
                self.assertEqual(text.startswith("NEED_CONFIRM:"), expect_confirm, text)

    def test_confirm_rechecks_execution_guard_before_execution(self):
        """§7.8：运行期收紧权限/用户取消仍由 guard 复核，确认过也不放行。"""
        snapshot_id = self.prepare_single()
        engine = self.executor("confirm")
        context = self.ctx()
        _ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], context)
        context["execution_guard"] = lambda: "授权配置已变化"
        ok2, out2 = engine.confirm_execute(self.confirm_id(out))
        self.assertFalse(ok2)
        self.assertIn("未执行", out2)
        self.assertEqual(self.task_count(), 0)

    def test_confirm_rechecks_expired_snapshot(self):
        """确认期间快照失效：批准后必须重新预检，不能拿旧快照生成。"""
        snapshot_id = self.prepare_single()
        engine = self.executor("confirm")
        _ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], self.ctx())
        path = self.root / "comfyui" / "dream-snapshots" / f"{snapshot_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["expires_at"] = 1
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        ok2, out2 = engine.confirm_execute(self.confirm_id(out))
        self.assertFalse(ok2)
        self.assertIn("过期", out2)
        self.assertEqual(self.task_count(), 0)

    def test_full_cannot_bypass_missing_dependency_tools(self):
        """§7.8：full 不能启用未勾选/不完整的能力；依赖门在 handler 里，不在权限档。"""
        snapshot_id = self.prepare_single()
        engine = self.executor("full")
        ok, out = engine.execute(
            "dream_submit", {"snapshot_id": snapshot_id}, [],
            self.ctx(allowed_tools=["dream_submit"]),
        )
        self.assertFalse(ok)
        self.assertIn("缺少", out)
        self.assertEqual(self.task_count(), 0)

    def test_fenced_action_still_asks_even_in_full(self):
        """围栏来源动作走显式确认，不经过 permission_mode，full 也不免。"""
        engine = self.executor("full")
        ok, out = engine.request_confirmation(
            "dream_submit", {"snapshot_id": "ds-forged"}, [], "围栏动作", self.ctx(),
        )
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)

    def test_wait_registration_does_not_add_generation_confirm(self):
        """§7.8：同一生成登记通知/等候不追加生成确认，也不重复生成。"""
        snapshot_id = self.prepare_single()
        engine = self.executor("auto")
        ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], self.ctx())
        self.assertTrue(ok, out)
        target = json.loads(out)["targets"][0]
        for mode in ("confirm", "auto", "full"):
            with self.subTest(mode=mode):
                engine = self.executor(mode)
                _ok, text = engine.execute("dream_task_wait", {"target_ids": [target]},
                                           [], self.ctx())
                self.assertFalse(text.startswith("NEED_CONFIRM:"), text)
        self.assertEqual(self.task_count(), 1, "登记等候绝不能触发第二次生成")

    def test_batch_submit_confirm_asks_once_and_creates_one_batch(self):
        """§7.8 B9：批量整批一张确认卡，批准后只建 1 个批次，不逐条再弹。"""
        snapshot_id = self.prepare_batch()
        engine = self.executor("confirm")
        # 批量提交要求 ComfyUI 探活通过（单次不要求）；harness 默认 reachable=None，
        # 这里临时改成在线，只放行探活，runner.submit 仍是打断网的桩。
        with mock.patch.object(client, "reachable", return_value={}):
            ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], self.ctx())
            self.assertFalse(ok)
            self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
            self.assertEqual(len(engine.pending_confirmation), 1,
                             "整批只登记 1 张确认卡，不按条目逐条弹")
            self.assertEqual(self.batch_count(), 0, "确认卡批准前绝不创建批次")
            self.assertEqual(self.task_count(), 0, "确认卡批准前不创建任何生成任务")

            confirm_id = self.confirm_id(out)
            ok2, out2 = engine.confirm_execute(confirm_id)
            self.assertTrue(ok2, out2)
            self.assertEqual(self.batch_count(), 1, "批准后整批只创建 1 个批次")
            payload = json.loads(out2)
            self.assertEqual(payload["kind"], "comfy-batch")
            batch = self.batches.get(payload["batch_id"])
            self.assertEqual(len(batch["items"]), 2, "两条条目都落在同一个批次里")

            ok3, out3 = engine.confirm_execute(confirm_id)
            self.assertFalse(ok3, "同一张批量确认卡只能消费一次")
            self.assertEqual(self.batch_count(), 1, "重复批准不得再建批次")

    def test_submit_confirm_card_shows_batch_summary(self):
        """§7.8 B9b：确认卡理由带出工作流 / 条目数 / 总次数 / 条目摘要。"""
        snapshot_id = self.prepare_batch()
        engine = self.executor("confirm")
        with mock.patch.object(client, "reachable", return_value={}):
            ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], self.ctx())
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        reason = out.split(":", 3)[2]
        self.assertIn("工作流", reason, reason)
        self.assertIn("对话验收流", reason, reason)
        self.assertIn("批量 2 条", reason, reason)
        self.assertIn("共 3 次", reason, reason)
        self.assertIn("甲×1", reason, reason)
        self.assertIn("乙×2", reason, reason)

    def test_submit_confirm_card_summary_missing_degrades_safely(self):
        """B9b：快照不存在时摘要退回仅工具名，不编造、不影响确认链。"""
        engine = self.executor("confirm")
        ok, out = engine.execute("dream_submit", {"snapshot_id": "ds-doesnotexist"}, [], self.ctx())
        self.assertFalse(ok)
        reason = out.split(":", 3)[2]
        self.assertEqual(reason, "执行工具需要确认：dream_submit")


class RaceAndRestartTests(AcceptanceHarness):
    """§8 第 8 条：越会话、重复提交、受理未知、结果晚到、通知竞态、重启。"""

    def test_cross_conversation_status_and_cancel_refused(self):
        snapshot_id = self.prepare_single()
        engine = self.executor("auto")
        ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], self.ctx())
        self.assertTrue(ok, out)
        target = json.loads(out)["task_id"]
        other = self.ctx(conversation_id="conv-2", owner_session_id="conv-2")
        _ok, status_text = engine.execute("dream_task_status", {"target_ids": [target]},
                                          [], other)
        self.assertIn("不是当前对话发起的", status_text)
        ok2, cancel_text = engine.execute("dream_task_cancel", {"target_id": target},
                                          [], other)
        self.assertFalse(ok2)
        self.assertIn("不是当前对话发起的", cancel_text)
        self.assertEqual(self.task_count(), 1)

    def test_replay_after_restart_returns_original_task(self):
        """重启 = 新建服务实例（存储不变）：同快照重放不得生成第二个任务。"""
        snapshot_id = self.prepare_single()
        first = DreamService(self.app).submit(snapshot_id)
        restarted = DreamService(self.app)
        second = restarted.submit(snapshot_id)
        self.assertTrue(second["already_submitted"])
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(self.task_count(), 1)

    def test_concurrent_replay_creates_single_task(self):
        """两个同时到达的重放：下游 request_key 幂等必须把它们收敛到同一任务。

        同时钉住回执落盘：并发写同一固定 .tmp 名会 PermissionError 并静默丢一次
        写入，回执是「不重复生成」的唯一凭据，不允许只是靠下游幂等侥幸兜住。
        """
        snapshot_id = self.prepare_single()
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[BaseException] = []

        def worker():
            try:
                barrier.wait(timeout=3)
                results.append(DreamService(self.app).submit(snapshot_id))
            except BaseException as exc:  # noqa: BLE001 - 子线程异常要带回主线程断言
                errors.append(exc)

        with mock.patch("naiba.comfyui.dream.logger.warning") as warning:
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(self.task_count(), 1, "并发重放不得生成两个任务")
        self.assertEqual(results[0]["task_id"], results[1]["task_id"])
        self.assertFalse(
            any("预检快照更新失败" in str(call) for call in warning.call_args_list),
            "并发回执写入不得撞临时文件名",
        )
        snapshot = DreamSnapshotStore(self.root).get(snapshot_id)
        self.assertTrue(snapshot["submitted"], "提交回执必须真的落盘")

    def test_notification_reuse_is_reported_and_does_not_duplicate(self):
        """通知登记竞态：同来源同目标复用同一条等待，不误报失败、不重复生成。"""
        snapshot_id = self.prepare_single()
        engine = self.executor("auto")
        ok, out = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], self.ctx())
        self.assertTrue(ok, out)
        self.waits.note = "reuse"
        ok2, out2 = engine.execute("dream_submit", {"snapshot_id": snapshot_id}, [], self.ctx())
        self.assertTrue(ok2, out2)
        payload = json.loads(out2)
        self.assertTrue(payload["notify_registered"], "reuse 也是登记成功")
        self.assertEqual(self.task_count(), 1)

    def test_late_native_result_is_not_reported_as_completed(self):
        """原生结果晚到：queued/running 不算终态，不能提前回报完成。"""
        queued = dream_provider._status_states(
            {"targets": [{"id": "comfy-task:cf-1", "task": {"status": "queued"}}]}
        )
        self.assertEqual(queued, [{"id": "comfy-task:cf-1", "terminal": False}])
        running_batch = dream_provider._status_states(
            {"targets": [{"id": "comfy-batch:cfb-1", "batch": {"items": [
                {"status": "completed"}, {"status": "running"},
            ]}}]}
        )
        self.assertEqual(running_batch, [{"id": "comfy-batch:cfb-1", "terminal": False}])

    def test_restart_pauses_wait_and_keeps_typed_target(self):
        """服务重启：等待一律暂停，类型化目标不丢、不自动再次派发。"""
        storage = ChatStorage(self.root / "chat.db")
        conversation_id = str(storage.create_conversation("造梦机重启验收")["id"])
        target = "comfy-task:cf-1"
        record, note = storage.create_job_wait(
            conversation_id=conversation_id, origin_run_id="run-1",
            target_job_ids=[target], snapshot={},
        )
        self.assertEqual(note, "")
        self.assertIsNotNone(record)
        self.assertEqual(storage.pause_job_waits_after_restart(), 1)
        paused = storage.get_job_wait(str(record["id"]))
        self.assertEqual(paused["status"], "needs_attention")
        self.assertEqual(paused["target_job_ids"], [target], "重启不得把类型化目标退化成裸 ID")
        self.assertEqual(parse_wait_target(target), ("comfy-task", "cf-1"))
        self.assertEqual(storage.list_job_waits(("dispatched",)), [],
                         "重启不得凭旧记录再次派发续答 Run")


if __name__ == "__main__":
    unittest.main()
