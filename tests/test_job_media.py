# -*- coding: utf-8 -*-
"""异步 Job 产物写回守门（P5：Job 终态把产物挂回发起它的助手消息）。

冻结的不变量：
1. 只对声明允许的 Job kind（comfyui/shell）写回；subagent/check/http_poll 不写
   （子 Agent 结论已随工具结果同步返回，重复写会双份展示）；
2. 只认终态且 result 非空（cancelled/failed 的部分产物同样要写回——"生成过就要能看到"）；
3. 归属链：job.parent_job_id → Run(kind∈chat/plan_execute) → 该会话里 metadata.run_id
   匹配的助手消息（优先 Run detail.message_id）；
4. 产物挂到**调用该 Job 的那次工具调用**（按 result 里的 job_id 匹配 tool_runs/activity），
   并重算消息级 attachments；匹配不到时并入 attachments（末尾网格仍可见）；
5. 幂等（按来源去重）；消息已删除/找不到时安全放弃；写回失败不抛给 Job 终态；
6. `conversations.updated_at` 必须推进——前端既有轮询据此重渲染，无需新事件通道。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.core.contracts import MetadataKeys  # noqa: E402
from naiba.core.media_types import job_media_declaration  # noqa: E402
from naiba.storage.job_media import JobMediaWriter  # noqa: E402
from naiba.storage.media_collect import MediaCollector  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


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


class JobMediaWriteBackTests(unittest.TestCase):
    RUN_ID = "run0001"
    JOB_ID = "job0001"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_job_media_"))
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = ChatStorage(self.data_dir / "chat.db")
        self.config = _ConfigStub(self.data_dir)
        self.collector = MediaCollector(self.config)
        self.writer = JobMediaWriter(self.storage, self.config, None, self.collector)
        self.conversation = self.storage.create_conversation("Job 产物冒烟")
        self.conversation_id = str(self.conversation["id"])
        self.storage.add_message(self.conversation_id, "user", "生成两张图")
        self.storage.add_message(
            self.conversation_id,
            "assistant",
            "已提交后台生成。",
            {
                "run_id": self.RUN_ID,
                "tool_runs": [
                    {
                        "tool": "comfyui_batch",
                        "success": True,
                        "result": json.dumps({"job_id": "pending", "status": "queued", "total": 2}),
                        "arguments": {"wait": False},
                    }
                ],
                "attachments": [],
            },
        )
        self.message = self.storage.get_conversation(self.conversation_id)["messages"][-1]
        self._insert_run(self.RUN_ID, kind="chat", detail={"message_id": str(self.message["id"])})
        self.png = self._make_png("seed.png")

    def _insert_run(self, run_id: str, kind: str, detail: dict) -> None:
        import time
        import uuid

        now = int(time.time() * 1000)
        with self.storage._connect() as db:  # noqa: SLF001 - 测试直接构造指定 id 的 Run 行
            db.execute(
                "INSERT INTO background_tasks(id, conversation_id, kind, message, agent_id, agent_name, "
                "status, snapshot, detail, created_at, updated_at, parent_job_id, owner_session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, self.conversation_id, kind, "生成两张图", "", "Agent",
                    "completed", "{}", json.dumps(detail, ensure_ascii=False), now, now, "", self.conversation_id,
                ),
            )
        del uuid

    def _make_png(self, name: str) -> Path:
        from PIL import Image

        path = self.tmp / name
        Image.new("RGB", (48, 32), (10, 120, 200)).save(path)
        return path

    def _create_job(
        self,
        kind: str = "comfyui",
        parent: str = RUN_ID,
        status: str = "completed",
        result: dict | None = None,
        record_job_id_in_message: bool = True,
    ) -> str:
        spec_agent = {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []}
        run = self.storage.create_run(
            self.conversation_id, "ComfyUI 批量生成", spec_agent, {}, kind=kind,
            parent_job_id=parent, owner_session_id=self.conversation_id,
        )
        job_id = str(run["id"])
        if record_job_id_in_message:
            # 真实链路里工具结果里就是真实 job_id（comfyui_batch wait=false 的返回）
            metadata = dict(self.message["metadata"])
            metadata["tool_runs"] = [dict(metadata["tool_runs"][0])]
            metadata["tool_runs"][0]["result"] = json.dumps({"job_id": job_id, "status": "queued"})
            self.storage.update_message_metadata(self.conversation_id, str(self.message["id"]), metadata)
        self.storage.update_job(
            job_id,
            status=status,
            result=result if result is not None else {"completed_shots": [{"index": 0, "files": [str(self.png)]}]},
            finished=True,
        )
        return job_id

    # ---- 正常路径 ----
    def test_comfyui_media_attached_to_originating_tool_run(self) -> None:
        job_id = self._create_job()
        written = self.writer.write_back(self.storage.get_background_task(job_id))
        self.assertIsNotNone(written)
        self.assertEqual(written["added"], 1)
        message = self.storage.get_conversation(self.conversation_id)["messages"][-1]
        metadata = message["metadata"]
        run = metadata["tool_runs"][0]
        self.assertEqual(len(run["media"]), 1)
        self.assertEqual(run["media"][0]["kind"], "image")
        self.assertEqual(len(metadata["attachments"]), 1)
        self.assertEqual(metadata["attachments"][0]["source"], run["media"][0]["source"])
        # 产物必须被托管缓存（可经 /api/file 服务），不是原始临时路径
        self.assertNotEqual(run["media"][0]["source"], str(self.png))
        self.assertTrue(Path(run["media"][0]["source"]).is_file())

    def test_conversation_updated_at_advanced(self) -> None:
        before = self.storage.get_conversation(self.conversation_id)["updated_at"]
        job_id = self._create_job()
        self.writer.write_back(self.storage.get_background_task(job_id))
        after = self.storage.get_conversation(self.conversation_id)["updated_at"]
        self.assertGreaterEqual(int(after), int(before))

    def test_idempotent_write_back(self) -> None:
        job_id = self._create_job()
        job = self.storage.get_background_task(job_id)
        self.assertIsNotNone(self.writer.write_back(job))
        self.assertIsNone(self.writer.write_back(job), "重复写回不得再产生新增")
        metadata = self.storage.get_conversation(self.conversation_id)["messages"][-1]["metadata"]
        self.assertEqual(len(metadata["tool_runs"][0]["media"]), 1)
        self.assertEqual(len(metadata["attachments"]), 1)

    def test_write_back_keeps_a_concurrently_written_session_start(self) -> None:
        """产物写回只 patch 自己那几个键：同一行的 `session_start` 不得被抹掉。

        真实路径：用户给某条 AI 回复点了「新会话」（`set_session_start` 就写在这一行），
        而那条回复发起的后台 Job 可能几十分钟后才跑完并写回产物 —— 旧实现拿着"点之前"
        的整块 metadata 整块写回，把 `session_start` 抹掉 ⇒ `build_model_history`
        清空整段上下文（用户视角＝模型突然失忆）。这里把并发写**精确注入在
        「读完整块、还没写回」的窗口里**：整块写回必红。
        """
        job_id = self._create_job()
        message_id = str(self.message["id"])
        real_attach = JobMediaWriter._attach
        injected = {"done": False}

        def attach_then_race(writer_self, metadata, job_id_arg, media, truncated):
            added = real_attach(writer_self, metadata, job_id_arg, media, truncated)
            if not injected["done"]:
                injected["done"] = True
                # 就在这个窗口里，另一个写入方落下自己的键（用户点了「新会话」）
                self.storage.set_session_start(self.conversation_id, message_id, note="窗口内")
            return added

        with mock.patch.object(JobMediaWriter, "_attach", attach_then_race):
            written = self.writer.write_back(self.storage.get_background_task(job_id))
        self.assertIsNotNone(written)
        metadata = self.storage.get_conversation(self.conversation_id)["messages"][-1]["metadata"]
        self.assertIn("session_start", metadata, "并发落下的会话边界不得被产物写回抹掉")
        self.assertEqual(len(metadata["tool_runs"][0]["media"]), 1, "产物仍然要写进去")
        self.assertEqual(len(metadata["attachments"]), 1, "消息级汇总也要更新")

    def test_concurrent_job_write_backs_keep_both_jobs_media(self) -> None:
        """两个 Job 并发写回同一条消息：读→合并→写必须在同一临界区。

        键级合并只隔离**不同键**；同一条消息的两次写回都改 `tool_runs`/`attachments`，
        后写者若拿着"读到的旧数组"覆盖先写者刚落的媒体，先写者的产物就没了（同键陈旧
        数组覆盖）。这里让两个 Job 各指向同一条消息的**一次工具调用**，两条线程同时
        写回，断言两份媒体都留下。
        """
        message_id = str(self.message["id"])
        job_a = self._create_job(record_job_id_in_message=False)
        second = self._make_png("second.png")
        job_b = self._create_job(
            record_job_id_in_message=False,
            result={"completed_shots": [{"index": 0, "files": [str(second)]}]},
        )
        # 两次工具调用各自 result 带自己的 job_id（真实链路里就是两次 comfyui_batch wait=false）
        metadata = dict(self.message["metadata"])
        metadata["tool_runs"] = [
            {"tool": "comfyui_batch", "success": True, "result": json.dumps({"job_id": job_a})},
            {"tool": "comfyui_batch", "success": True, "result": json.dumps({"job_id": job_b})},
        ]
        metadata["attachments"] = []
        self.storage.update_message_metadata(self.conversation_id, message_id, metadata)

        real_attach = JobMediaWriter._attach

        def slow_attach(writer_self, meta, job_id_arg, media, truncated):
            added = real_attach(writer_self, meta, job_id_arg, media, truncated)
            time.sleep(0.4)  # 拉大"读后未写"的窗口：没有串行化时两个写回必然都基于旧快照
            return added

        sink: dict[str, Any] = {}

        def run(job_id: str, key: str) -> None:
            try:
                sink[key] = self.writer.write_back(self.storage.get_background_task(job_id))
            except BaseException as exc:  # noqa: BLE001 - 线程里不能吞掉异常，要回传给主线程断言
                sink[key] = exc

        with mock.patch.object(JobMediaWriter, "_attach", slow_attach):
            t1 = threading.Thread(target=run, args=(job_a, "a"))
            t2 = threading.Thread(target=run, args=(job_b, "b"))
            t1.start()
            time.sleep(0.08)  # 让 A 先读到旧快照，再放 B 进来
            t2.start()
            t1.join(20)
            t2.join(20)
        self.assertFalse(t1.is_alive(), "写回线程未在超时内结束")
        self.assertFalse(t2.is_alive(), "写回线程未在超时内结束")
        for key in ("a", "b"):
            value = sink.get(key)
            self.assertIsInstance(value, dict, f"写回 {key} 应成功：{value!r}")
            self.assertEqual(value["added"], 1)

        metadata = self.storage.get_conversation(self.conversation_id)["messages"][-1]["metadata"]
        runs = metadata["tool_runs"]
        self.assertEqual(len(runs[0].get("media") or []), 1, "Job A 的产物被并发写回覆盖了")
        self.assertEqual(len(runs[1].get("media") or []), 1, "Job B 的产物被并发写回覆盖了")
        self.assertEqual(len(metadata["attachments"]), 2, "消息级汇总应同时含两份产物")

    def test_write_back_clears_a_stale_truncated_marker(self) -> None:
        """产物不再截断时要清掉旧的 `attachments_truncated`（改走 json_remove 的行为回归）。

        整块写回是靠 `metadata.pop(...)` 清这个键的；改成键级 patch 后必须显式用
        `remove=`，否则界面上会留着一个过期的"已截断"提示。
        """
        job_id = self._create_job()
        message_id = str(self.message["id"])
        self.assertTrue(self.storage.merge_message_metadata(
            self.conversation_id, message_id,
            {MetadataKeys.ATTACHMENTS_TRUNCATED: {"images": 9}},
        ))
        self.assertIsNotNone(self.writer.write_back(self.storage.get_background_task(job_id)))
        metadata = self.storage.get_conversation(self.conversation_id)["messages"][-1]["metadata"]
        self.assertNotIn(MetadataKeys.ATTACHMENTS_TRUNCATED, metadata)
        self.assertEqual(len(metadata["attachments"]), 1)

    def test_cancelled_job_partial_products_written(self) -> None:
        job_id = self._create_job(status="cancelled")
        written = self.writer.write_back(self.storage.get_background_task(job_id))
        self.assertIsNotNone(written)
        self.assertEqual(written["added"], 1)

    # ---- 不写回的分支 ----
    def test_job_without_parent_run(self) -> None:
        job_id = self._create_job(parent="")
        self.assertIsNone(self.writer.write_back(self.storage.get_background_task(job_id)))

    def test_non_chat_parent_ignored(self) -> None:
        job_id = self._create_job(parent=self.JOB_ID)  # 父不是 chat/plan_execute
        self.assertIsNone(self.writer.write_back(self.storage.get_background_task(job_id)))

    def test_subagent_and_check_kinds_not_written(self) -> None:
        for kind in ("subagent", "check", "http_poll"):
            with self.subTest(kind=kind):
                job_id = self._create_job(kind=kind)
                self.assertIsNone(self.writer.write_back(self.storage.get_background_task(job_id)))

    def test_empty_result_not_written(self) -> None:
        job_id = self._create_job(result={})
        self.assertIsNone(self.writer.write_back(self.storage.get_background_task(job_id)))

    def test_missing_message_is_noop(self) -> None:
        self.storage.clear_conversation_messages(self.conversation_id)
        job_id = self._create_job()
        self.assertIsNone(self.writer.write_back(self.storage.get_background_task(job_id)))

    # ---- 匹配不到工具调用时退化为消息级附件 ----
    def test_unmatched_job_id_falls_back_to_attachments(self) -> None:
        message = self.storage.add_message(
            self.conversation_id,
            "assistant",
            "另一轮回复",
            {"run_id": "run0002", "tool_runs": [{"tool": "pwsh", "success": True, "result": "无 job"}], "attachments": []},
        )
        self._insert_run("run0002", kind="chat", detail={"message_id": str(message["id"])})
        job_id = self._create_job(parent="run0002", record_job_id_in_message=False)
        written = self.writer.write_back(self.storage.get_background_task(job_id))
        self.assertIsNotNone(written)
        metadata = [m for m in self.storage.get_conversation(self.conversation_id)["messages"] if m["id"] == message["id"]][0]["metadata"]
        self.assertEqual(metadata["tool_runs"][0].get("media"), None)
        self.assertEqual(len(metadata["attachments"]), 1)


class JobRegistryWriteBackWiringTests(unittest.TestCase):
    """`JobRegistry._finish` 必须触发写回（接线守门）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_job_media_wire_"))
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = ChatStorage(self.data_dir / "chat.db")
        self.config = _ConfigStub(self.data_dir)
        self.writer = JobMediaWriter(self.storage, self.config, None, MediaCollector(self.config))
        self.bus = _BusStub()

        class _App:
            storage = self.storage
            event_bus = self.bus
            job_media_writer = self.writer

        self.app = _App()

    def test_finish_triggers_write_back(self) -> None:
        from PIL import Image

        from naiba.jobs import JobRegistry

        conversation = self.storage.create_conversation("接线")
        conversation_id = str(conversation["id"])
        message = self.storage.add_message(
            conversation_id, "assistant", "已提交",
            {"run_id": "runW", "tool_runs": [{"tool": "comfyui_batch", "success": True, "result": json.dumps({"job_id": "JOBW"})}], "attachments": []},
        )
        import time

        now = int(time.time() * 1000)
        with self.storage._connect() as db:  # noqa: SLF001
            db.execute(
                "INSERT INTO background_tasks(id, conversation_id, kind, message, agent_id, agent_name, "
                "status, snapshot, detail, created_at, updated_at, parent_job_id, owner_session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("runW", conversation_id, "chat", "已提交", "", "Agent", "running", "{}",
                 json.dumps({"message_id": str(message["id"])}, ensure_ascii=False), now, now, "", conversation_id),
            )
        png = self.tmp / "wire.png"
        Image.new("RGB", (24, 16), (0, 90, 180)).save(png)
        run = self.storage.create_run(
            conversation_id, "ComfyUI 批量生成", {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []},
            {}, kind="comfyui", parent_job_id="runW", owner_session_id=conversation_id,
        )
        job_id = str(run["id"])
        with self.storage._connect() as db:  # noqa: SLF001 - 让工具结果带上真实 job id
            db.execute("UPDATE messages SET metadata = ? WHERE id = ?", (
                json.dumps({
                    "run_id": "runW",
                    "tool_runs": [{"tool": "comfyui_batch", "success": True, "result": json.dumps({"job_id": job_id})}],
                    "attachments": [],
                }, ensure_ascii=False),
                str(message["id"]),
            ))

        registry = JobRegistry(self.app)
        registry._finish(job_id, "completed", result={"completed_shots": [{"index": 0, "files": [str(png)]}]})
        metadata = [m for m in self.storage.get_conversation(conversation_id)["messages"] if m["id"] == message["id"]][0]["metadata"]
        self.assertEqual(len(metadata["tool_runs"][0]["media"]), 1, "Job 终态必须触发产物写回")
        self.assertTrue(any(payload.get("type") == "job_finished" for _, payload in self.bus.events))


class JobMediaDeclarationTests(unittest.TestCase):
    def test_job_declarations(self) -> None:
        self.assertEqual(job_media_declaration("comfyui"), {"policy": "inline", "extract": "structured"})
        self.assertEqual(job_media_declaration("shell"), {"policy": "inline", "extract": "scan"})
        for kind in ("subagent", "check", "http_poll", "unknown"):
            with self.subTest(kind=kind):
                self.assertEqual(job_media_declaration(kind), {"policy": "never", "extract": "none"})


if __name__ == "__main__":
    unittest.main()
