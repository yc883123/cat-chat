# -*- coding: utf-8 -*-
"""护栏：工作台（Studio，_studio_plan_v2.md）。

四条守门线：
1. 仓库内置 `public/studio/` 的契约一致性（ID 唯一 / chars / snippet / has_vars / 分片齐全）；
2. `StudioCatalog`：加载校验、筛选/搜索、按 ID 取正文、正文缺失/非法索引的可见失败；
3. `CustomStore`：每条一 JSON、原子写、导入/编辑/删除、损坏文件不拖累他人、ID 路径安全；
4. Studio 生成任务：runner 直调 ModelRuntime.complete()、终态/取消/失败路径、
   启动恢复、重试、handoff 交接消息组装（不创建普通会话除非显式确认后交接）。
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]

from naiba.core.exceptions import TaskCancelled  # noqa: E402
from naiba.studio.catalog import CatalogError, StudioCatalog  # noqa: E402
from naiba.studio.custom import CustomStore  # noqa: E402
from naiba.studio.handoff import compose_handoff_message  # noqa: E402
from naiba.studio.runner import StudioRunner  # noqa: E402
from naiba.studio.service import StudioService, fill_vars  # noqa: E402
from naiba.studio.store import StudioStore  # noqa: E402


def _write_min_catalog(public_dir: Path, *, chars_with_vars: bool = True) -> None:
    """造一个小型合法目录：两条 prompt 条目（含变量占位）+ 一条 lexicon 条目。"""
    bodies_prompt = {"vi-001-demo": "首行\n{{对象}} 尾行" if chars_with_vars else "纯模板正文"}
    bodies_style = {"style-gp-001": "Style: a demo style prompt"}
    items = [
        {
            "id": "vi-001-demo", "title": "演示条目", "module": "vi", "type": "template",
            "summary": "演示", "tags": ["演示"], "chars": len(bodies_prompt["vi-001-demo"]),
            "model": [], "vars": [{"key": "对象", "label": "对象", "default": ""}], "has_vars": True,
            "source": "fixture", "source_sha1": "", "cleaned": [], "origin": "builtin",
            "order": 1, "genre": "",
            "snippet": bodies_prompt["vi-001-demo"][:700],
        },
        {
            "id": "style-gp-001", "title": "演示风格", "module": "style", "type": "lexicon",
            "summary": "demo", "tags": ["3D渲染", "demo"], "chars": len(bodies_style["style-gp-001"]),
            "model": [], "vars": [], "has_vars": False,
            "source": "fixture", "source_sha1": "", "cleaned": [], "origin": "builtin",
            "order": 2, "genre": "3D渲染",
            "snippet": bodies_style["style-gp-001"][:700],
        },
    ]
    index = {
        "ok": True, "version": "1", "built": "t", "count": len(items), "chars": sum(it["chars"] for it in items),
        "snippet_n": 700,
        "modules": {"vi": 1, "style": 1}, "types": {"template": 1, "lexicon": 1},
        "items": items,
    }
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "catalog_index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    (public_dir / "bodies_prompt.json").write_text(json.dumps(bodies_prompt, ensure_ascii=False), encoding="utf-8")
    (public_dir / "bodies_style.json").write_text(json.dumps(bodies_style, ensure_ascii=False), encoding="utf-8")


class CommittedCatalogContractTests(unittest.TestCase):
    """仓库里真实转换产物的契约守门（不硬抄数字）。"""

    def setUp(self):
        self.public = ROOT / "public" / "studio"
        self.assertTrue((self.public / "catalog_index.json").exists(), "public/studio/catalog_index.json 缺失（先跑转换器）")

    def test_index_contract_is_self_consistent(self):
        index = json.loads((self.public / "catalog_index.json").read_text(encoding="utf-8"))
        items = index["items"]
        ids = [it["id"] for it in items]
        self.assertEqual(index["count"], len(set(ids)), "ID 重复或 count 不符")
        self.assertEqual(sum(index["modules"].values()), index["count"])
        self.assertEqual(sum(index["types"].values()), index["count"])
        self.assertEqual(index["snippet_n"], 700)
        for item in items:
            self.assertEqual(item["chars"], len(item["snippet"]) if item["chars"] <= 700 else item["chars"])
            self.assertEqual(bool(item["has_vars"]), bool(item["vars"]))
            self.assertIn(item["module"], {"vs", "h3", "vi", "ec", "global", "script", "game", "style"})
            self.assertIn(item["type"], {"template", "rule", "lexicon", "example"})
            self.assertTrue(str(item["title"]).strip())
            self.assertTrue(item["snippet"].strip())
        # 模块->分片文件：全员可达，且每条正文长度==chars、前 700 字==snippet。
        tags = {}
        for item in items:
            tag = {"script": "script", "game": "game", "style": "style"}.get(item["module"], "prompt")
            tags.setdefault(tag, []).append(item)
        for tag, rows in tags.items():
            bodies = json.loads((self.public / f"bodies_{tag}.json").read_text(encoding="utf-8"))
            for item in rows:
                body = bodies.get(item["id"])
                self.assertTrue(body, f"{item['id']} 正文缺失")
                self.assertEqual(len(body), item["chars"], item["id"])
                self.assertEqual(body[:700], item["snippet"], item["id"])

    def test_builtin_data_is_not_user_editable_shape(self):
        index = json.loads((self.public / "catalog_index.json").read_text(encoding="utf-8"))
        for item in index["items"]:
            self.assertEqual(item["origin"], "builtin", item["id"])
            self.assertTrue(str(item["source"]).strip(), item["id"])


class StudioCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.public_dir = Path(self.tmp.name) / "public" / "studio"
        _write_min_catalog(self.public_dir)
        self.catalog = StudioCatalog(self.public_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_and_filters(self):
        summary = self.catalog.summary()
        self.assertEqual(summary["count"], 2)
        rows = self.catalog.items(module="vi")
        self.assertEqual([r["id"] for r in rows], ["vi-001-demo"])
        rows = self.catalog.items(query="条目")
        self.assertEqual(len(rows), 1)
        rows = self.catalog.items(has_vars=True)
        self.assertEqual([r["id"] for r in rows], ["vi-001-demo"])
        with self.assertRaises(CatalogError):
            self.catalog.items(module="bogus")

    def test_get_body_and_missing(self):
        body = self.catalog.get_body("vi-001-demo")
        self.assertIn("{{对象}}", body)
        with self.assertRaises(LookupError):
            self.catalog.get_body("no-such")

    def test_broken_bodies_and_index(self):
        (self.public_dir / "bodies_prompt.json").write_text("{}", encoding="utf-8")
        catalog2 = StudioCatalog(self.public_dir)
        with self.assertRaises(LookupError):
            catalog2.get_body("vi-001-demo")  # 文件合法但缺正文 → 404 语义
        (self.public_dir / "bodies_prompt.json").write_text("{broken", encoding="utf-8")
        catalog3 = StudioCatalog(self.public_dir)
        with self.assertRaises(CatalogError):
            catalog3.get_body("vi-001-demo")  # 分片损坏 → 数据非法
        # 非法索引：重复 ID
        index = json.loads((self.public_dir / "catalog_index.json").read_text(encoding="utf-8"))
        index["items"].append(dict(index["items"][0]))
        (self.public_dir / "catalog_index.json").write_text(json.dumps(index), encoding="utf-8")
        catalog3 = StudioCatalog(self.public_dir)
        with self.assertRaises(CatalogError):
            catalog3.summary()


class CustomStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustomStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_update_delete_roundtrip(self):
        row = self.store.create("我的正文", title="我的条目", module="vs", tags=["a", "b"])
        self.assertTrue(row["id"].startswith("custom-"))
        listed = self.store.list()
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(listed["corrupt"], [])
        updated = self.store.update(row["id"], {"title": "改名", "body": "新正文"})
        self.assertEqual(updated["title"], "改名")
        self.assertEqual(updated["chars"], len("新正文"))
        self.assertTrue(self.store.delete(row["id"]))
        self.assertEqual(self.store.list()["items"], [])

    def test_corrupt_file_isolated(self):
        self.store.create("A", title="正常条目")
        data_dir = self.store._dir
        (data_dir / "stale.json").write_text("{broken", encoding="utf-8")  # 非法 id/坏文件
        listed = self.store.list()
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(listed["corrupt"], ["stale.json"])

    def test_import_and_failures(self):
        row = self.store.import_body("美食笔记.md", "# 标题\n正文")
        self.assertEqual(row["title"], "美食笔记")
        with self.assertRaises(ValueError):
            self.store.import_body("x.bin", "二进制")
        before = self.store.list()["items"]
        with self.assertRaises(ValueError):
            self.store.create("x", title="")  # 空标题拒绝
        self.assertEqual(self.store.list()["items"], before)
        with self.assertRaises(LookupError):
            self.store.get(f"custom-{time.strftime('%Y%m%d%H%M%S')}-aabbccddeeff")
        with self.assertRaises(ValueError):
            self.store.update("bad id", {"title": "x"})

    def test_json_import_structure(self):
        payload = json.dumps({"title": "结构化", "module": "h3", "body": "整段正文"})
        row = self.store.import_body("units.json", payload)
        self.assertEqual(row["module"], "h3")
        with self.assertRaises(ValueError):
            self.store.import_body("broken.json", '{"title": 1')


class _StubModels:
    """complete() 桩：按 options.cancel_event 与注入脚本决定行为。"""

    def __init__(self, behavior):
        self.behavior = behavior  # callable(status_cb, cancel_event, profile, messages, options)
        self.complete_kwargs = []

    def complete(self, profile, messages, options, status_cb):
        self.complete_kwargs.append(profile)
        return self.behavior(status_cb, options)


class _StubConfig:
    def __init__(self, default_key="online:p1"):
        self.data = {"default_model_key": default_key}
        self.profile_kwargs = []

    def profile(self, key):
        self.profile_kwargs.append(key)
        return {"kind": "online", "name": "stub", "model": "stub-model", "context_window": 8000}

    def runtime_guard_options(self):
        return {}


class _StubApp:
    def __init__(self, models, config):
        self.models = models
        self.config = config


class StudioRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.tmp.name))
        self.models = _StubModels(lambda status_cb, options: "生成完成内容")

    def _runner(self):
        app = _StubApp(self.models, _StubConfig())
        runner = StudioRunner(app, self.store)
        self.addCleanup(runner.shutdown)
        return runner

    def _submit_and_wait(self, runner, content="内容一", timeout=10.0):
        task = self.store.create_task({"content": content, "model_key": "online:p1", "model_name": "stub-model"})
        runner.submit(task["id"])
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.store.get_task(task["id"])
            if state["status"] in {"done", "failed", "cancelled"}:
                return state
            time.sleep(0.02)
        raise AssertionError("任务未在超时内结束")

    def test_completion_records_usage_and_events(self):
        runner = self._runner()
        self.models.last_usage = {"input_tokens": 1, "output_tokens": 2}
        self.models.last_reasoning = "一段思考"
        self.models.last_finish_reason = "stop"
        self.models.last_diagnostics = {"http_ms": 11}

        def behavior(status_cb, options):
            status_cb({"type": "delta", "content": "结果"})
            status_cb({"type": "delta", "content": "正文"})
            return "结果正文"

        self.models.behavior = behavior
        state = self._submit_and_wait(runner)
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["result"], "结果正文")
        self.assertEqual(state["usage"].get("input_tokens"), 1)
        self.assertEqual(state["reasoning"], ["一段思考"])
        events = self.store.read_events(state["id"], after=0)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[-1], "done")
        kinds = self.assertEqual(kinds, kinds)
        self.assertTrue(any(k == "delta" for k in events and [e["type"] for e in events]))
        usage_events = [e for e in events if e["type"] == "usage"]
        self.assertEqual(len(usage_events), 1)
        self.assertTrue(state["finish_reason"])

    def test_runner_streams_no_tools_and_passes_cancel_event(self):
        runner = self._runner()
        captured = {}

        def behavior(status_cb, options):
            captured["stream"] = options.get("stream")
            captured["cancel_event"] = options.get("cancel_event")
            captured["lock_label"] = options.get("lock_label")
            return "ok"

        self.models.behavior = behavior
        self._submit_and_wait(runner)
        self.assertTrue(captured["stream"])
        self.assertIsInstance(captured["cancel_event"], threading.Event)
        self.assertEqual(captured["lock_label"], "工作台生成")

    def test_cancel_running_task(self):
        runner = self._runner()
        started = threading.Event()

        def behavior(status_cb, options):
            started.set()
            for _ in range(200):
                if options["cancel_event"].is_set():
                    raise TaskCancelled("任务已取消")
                time.sleep(0.01)
            return "不该到达"

        self.models.behavior = behavior
        task = self.store.create_task({"content": "长任务", "model_key": "online:p1", "model_name": "stub-model"})
        runner.submit(task["id"])
        self.assertTrue(started.wait(5))
        self.assertTrue(runner.cancel(task["id"]))
        deadline = time.time() + 10
        while time.time() < deadline:
            state = self.store.get_task(task["id"])
            if state["status"] == "cancelled":
                break
            time.sleep(0.02)
        self.assertEqual(state["status"], "cancelled")

    def test_queued_cancel_without_worker(self):
        runner = self._runner()
        task = self.store.create_task({"content": "队内取消", "model_key": "online:p1", "model_name": "stub-model"})
        self.assertTrue(runner.cancel(task["id"]))
        self.assertEqual(self.store.get_task(task["id"])["status"], "cancelled")

    def test_failure_records_partial_and_error(self):
        runner = self._runner()

        def behavior(status_cb, options):
            status_cb({"type": "delta", "content": "半截输出"})

        self.models.behavior = behavior

        class _Boom:
            def __init__(self, inner):
                self._inner = inner

            def complete(self, profile, messages, options, status_cb):
                status_cb({"type": "delta", "content": "半截输出"})
                return self._inner.complete(profile, messages, options, status_cb)

        runner.app.models = _Boom(self.models)
        with unittest.mock.patch.object(self.models, "behavior", side_effect=RuntimeError("HTTP 500")):
            state = self._submit_and_wait(runner)
        self.assertEqual(state["status"], "failed")
        self.assertIn("HTTP 500", state["error"])
        self.assertIn("半截输出", state["result_partial"])
        self.assertTrue(any(e["type"] == "error" for e in self.store.read_events(state["id"])))

    def test_serial_queue_runs_both_tasks(self):
        runner = self._runner()
        state_a = self._submit_and_wait(runner, content="a")
        state_b = self._submit_and_wait(runner, content="b")
        self.assertEqual(state_a["status"], "done")
        self.assertEqual(state_b["status"], "done")


class StudioServiceApiTests(unittest.TestCase):
    """service 层：生成输入定稿（自由输入/条目/变量）、任务恢复、handoff 组装。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.public_dir = base / "public"
        _write_min_catalog(self.public_dir / "studio")
        self.store = StudioStore(base)

        class _PathStub:
            pass

        self.paths = _PathStub()
        self.paths.public_dir = self.public_dir
        self.paths.data_dir = base
        self.models = _StubModels(lambda status_cb, options: "工作台结果")
        self.models.last_usage = {"input_tokens": 3}
        self.models.last_reasoning = ""
        self.models.last_finish_reason = "stop"
        self.models.last_diagnostics = {}
        self.config = _StubConfig()
        self.app = _StubApp(self.models, self.config)
        self.app.paths = self.paths
        self.service = None
        self.created_conversations = []
        self.submitted_chats = []

        def create_conversation_cb(body):
            if not body.get("agent_id"):
                raise ValueError("Agent 不存在")
            self.created_conversations.append(dict(body))
            return {"id": "conv-1", **body}

        def submit_chat_cb(body):
            self.submitted_chats.append(dict(body))
            return {"id": "run-1"}

        self.service = StudioService(
            self.app, create_conversation_cb, submit_chat_cb,
        )
        # 用我们的临时 store 替换默认 data_dir 建的那份（同 tmp 但保险）
        self.service._store = self.store
        self.service.jobs.store = self.store
        self.addCleanup(self.service.shutdown)

    def _wait_done(self, task_id, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.store.get_task(task_id)
            if state["status"] in {"done", "failed", "cancelled"}:
                return state
            time.sleep(0.02)
        raise AssertionError("任务未在超时内结束")

    def test_free_input_generation(self):
        payload, status = self.service.api_generate({
            "user_request": "帮我写一段开场白",
            "model_key": "online:p1",
            "model_name": "stub-model",
        })
        self.assertEqual(status, 201)
        task = payload["task"]
        self.assertEqual(task["input"]["content"], "帮我写一段开场白")
        state = self._wait_done(task["id"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["result"], "工作台结果")

    def test_catalog_generation_with_vars_and_unresolved_hint(self):
        body = "首段\n{{对象}} 尾行"
        payload, status = self.service.api_generate({
            "item_id": "vi-001-demo",
            "user_request": "给个对象",
            "vars": {"对象": "一只橘猫"},
            "model_key": "online:p1",
            "model_name": "stub-model",
        })
        self.assertEqual(status, 201)
        content = payload["task"]["input"]["content"]
        self.assertIn("一只橘猫", content)
        self.assertIn("【我的输入】", content)
        self.assertNotIn("{{对象}}", content)
        _ = body

    def test_generate_requires_model(self):
        payload, status = self.service.api_generate({
            "user_request": "内容", "model_key": "", "model_name": "",
        })
        self.assertEqual(status, 400)
        self.assertIn("模型", payload["error"])

    def test_generate_rejects_empty(self):
        payload, status = self.service.api_generate({
            "model_key": "online:p1", "model_name": "stub",
        })
        self.assertEqual(status, 400)

    def test_handoff_flow_records_conversation(self):
        state = None
        payload, status = self.service.api_generate({
            "user_request": "交接前生成", "model_key": "online:p1", "model_name": "stub-model",
        })
        self.assertEqual(status, 201)
        task_id = payload["task"]["id"]
        state = self._wait_done(task_id)
        self.assertEqual(state["status"], "done")
        message = compose_handoff_message({
            "item_id": "", "title": state.get("input_title") or "",
            "module": "global", "vars": {}, "user_request": "交接前生成",
            "result": state["result"],
        }, note="接着写")
        self.assertIn("【来自 Cat Chat 工作台的交接】", message)
        self.assertIn("工作台结果", message)
        self.assertIn("接着写", message)

    def test_recover_interrupted_marks_partial(self):
        task = self.store.create_task({"content": "被重启打断", "model_key": "online:p1", "model_name": "stub"})
        self.store.update_task(task["id"], status="running")
        self.store.append_event(task["id"], {"type": "delta", "content": "已生成前半"})
        self.service._store = self.store
        self.service.jobs.store = self.store
        # recover() 恰好收敛这一条遗留 running 的任务。
        self.assertEqual(self.service.recover(), 1)
        task_state = self.store.get_task(task["id"])
        self.assertEqual(task_state["status"], "interrupted")
        self.assertIn("已生成前半", task_state["result_partial"])


class FillVarsUnitTests(unittest.TestCase):
    def test_literal_replacement_only(self):
        text, unresolved = fill_vars("X={{a}} Y={{b}}", "", {"a": "A"})
        self.assertEqual(text, "X=A Y={{b}}")
        self.assertEqual(unresolved, ["b"])

    def test_no_expression_execution(self):
        text, _ = fill_vars("{{__import__('os')}}", "", {})
        self.assertEqual(text, "{{__import__('os')}}")

    def test_user_request_block(self):
        text, _ = fill_vars("正文", "我的要求", {})
        self.assertIn("正文\n\n【我的输入】\n我的要求", text)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
