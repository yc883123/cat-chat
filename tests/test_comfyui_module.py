# -*- coding: utf-8 -*-
"""护栏：造梦机（docs/plans/2026-10-23-ComfyUI自动化工作台模块计划.md）。

覆盖六条主线（全部不联网，ComfyUI 用假 client）：
1. 参数扫描：槽位类型、连线跳过、seed/image 标记、原值类型保真应用；
2. 导入契约：API 格式放行、UI 格式带导出步骤拒绝、wrapper 兼容；
3. 存储：工作流/任务 CRUD、checkpoint 白名单、事件游标、产物目录删除；
4. 执行：两段任务逐段推进、提交失败带服务端原因、历史错误、丢失重提交预算、
   取消两步语义、恢复收养（已完成段不重复提交）、启动恢复；
5. 任务管理：overrides 校验、重试续跑、批量删除跳过执行中、删除工作流保护；
6. 门面：导入/详情/文件路径安全。
"""

import hashlib
import inspect
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.comfyui import client as comfyui_client  # noqa: E402
from naiba.comfyui import PARAMS_SCHEMA_VERSION  # noqa: E402
from naiba.comfyui.jobs import ComfyUIJobManager, ComfyUITaskError  # noqa: E402
from naiba.comfyui.params import apply_overrides, scan_params  # noqa: E402
from naiba.comfyui.runner import ComfyUIRunner  # noqa: E402
from naiba.comfyui.service import (  # noqa: E402
    ComfyUIService,
    UI_FORMAT_ERROR,
    normalize_api_workflow,
)
from naiba.comfyui.store import ComfyUIStore  # noqa: E402


def _sample_prompt() -> dict:
    return {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "model.safetensors"}, "_meta": {"title": "模型"}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "一只猫", "clip": ["4", 1]}, "_meta": {"title": "正向提示词"}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": 123, "steps": 20, "cfg": 7.5,
                         "sampler_name": "euler", "scheduler": "normal",
                         "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["5", 0]}, "_meta": {"title": "采样"}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "8": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "out", "images": ["9", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "bad", "clip": ["4", 1]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "10": {"class_type": "LoraLoader",
               "inputs": {"lora_name": "my.safetensors", "strength_model": 1.0,
                          "strength_clip": 1.0, "model": ["4", 0], "clip": ["4", 1]},
               "_meta": {"title": "LoRA"}},
    }


class _StubPaths:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)


class _StubApp:
    def __init__(self, data_dir: Path, comfyui_url: str = "http://127.0.0.1:9"):
        self.paths = _StubPaths(data_dir)
        self.config = type("C", (), {
            "data": {"comfyui_url": comfyui_url},
            "save": lambda self: None,
        })()


class _FakeResponse:
    """context manager 形状的假 HTTP 响应（供 _DIRECT_OPENER 打桩）。"""

    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CheckpointNamesTests(unittest.TestCase):
    """模型候选提取：解析 /object_info 单节点响应；失败一律空列表。"""

    def test_parses_enum_choices_deduped_and_sorted(self):
        payload = {"CheckpointLoaderSimple": {"input": {"required": {
            "ckpt_name": [["b.safetensors", "a.safetensors", "a.safetensors"], {}]}}}}
        with mock.patch.object(comfyui_client, "_DIRECT_OPENER") as opener:
            opener.open.return_value = _FakeResponse(payload)
            names = comfyui_client.checkpoint_names("http://127.0.0.1:1")
        self.assertEqual(names, ["a.safetensors", "b.safetensors"])

    def test_failure_returns_empty(self):
        with mock.patch.object(comfyui_client, "_DIRECT_OPENER") as opener:
            opener.open.side_effect = OSError("down")
            self.assertEqual(comfyui_client.checkpoint_names("http://127.0.0.1:1"), [])
            self.assertEqual(comfyui_client.lora_names("http://127.0.0.1:1"), [])

    def test_malformed_payload_returns_empty(self):
        payloads = ([], {"CheckpointLoaderSimple": {}},
                    {"CheckpointLoaderSimple": {"input": {}}},
                    {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": []}}}})
        for payload in payloads:
            with mock.patch.object(comfyui_client, "_DIRECT_OPENER") as opener:
                opener.open.return_value = _FakeResponse(payload)
                self.assertEqual(comfyui_client.checkpoint_names("http://127.0.0.1:1"), [],
                                 payload)

    def test_lora_names_merge_both_loaders(self):
        # 两个标准 LoRA 加载器的 lora_name 枚举合并去重（同目录内容一致，
        # 工作流用的是第三方加载器时也能给对候选）。
        payloads = iter([
            {"LoraLoader": {"input": {"required": {
                "lora_name": [["b.safetensors", "a.safetensors"], {}]}}}},
            {"LoraLoaderModelOnly": {"input": {"required": {
                "lora_name": [["c.safetensors", "a.safetensors"], {}]}}}},
        ])
        with mock.patch.object(comfyui_client, "_DIRECT_OPENER") as opener:
            opener.open.side_effect = lambda url, timeout=10: _FakeResponse(next(payloads))
            names = comfyui_client.lora_names("http://127.0.0.1:1")
        self.assertEqual(names, ["a.safetensors", "b.safetensors", "c.safetensors"])


class ParamsTests(unittest.TestCase):
    def test_client_uses_no_proxy_opener(self):
        # 用户拍板（2026-10-24）：连 ComfyUI 一律直连，不吃任何代理——
        # 运行设置代理、系统代理、环境变量都不影响，与地址形状无关。
        # 空代理表的 ProxyHandler 没有 *_open 方法，build_opener 会把它丢出
        # 处理链——最终效果就是链上没有代理处理器，任何请求都直连。
        proxy_handlers = [h for h in comfyui_client._DIRECT_OPENER.handlers
                          if isinstance(h, urllib.request.ProxyHandler)]
        for handler in proxy_handlers:
            self.assertEqual(handler.proxies, {})
        # 回归警戒线：不许退回 urlopen（默认 opener 会读环境/系统代理）或 net_io 代理策略。
        source = inspect.getsource(comfyui_client)
        self.assertNotIn("urlopen", source)
        self.assertNotIn("net_io.open", source)

    def test_scan_types_and_connection_skip(self):
        slots = scan_params(_sample_prompt())
        kinds = {(s["node_id"], s["field_name"]): s["field_type"] for s in slots}
        self.assertEqual(kinds[("3", "seed")], "seed")
        self.assertEqual(kinds[("3", "cfg")], "number")
        self.assertEqual(kinds[("6", "text")], "text")
        self.assertEqual(kinds[("5", "batch_size")], "number")
        # 连线值（数组）不是控件槽位。
        for connection in (("6", "clip"), ("8", "images"), ("3", "model")):
            self.assertNotIn(connection, kinds)
        # 槽位标签带节点标题（按节点定位：两个节点都有 text 字段，不能按字段名折叠）。
        labels = {(s["node_id"], s["field_name"]): s["label"] for s in slots}
        self.assertIn("正向提示词", labels[("6", "text")])
        self.assertIn("采样", labels[("3", "seed")])

    def test_prompt_roles_traced_from_sampler_connections(self):
        # KSampler 的 positive/negative 连线回溯：节点 6 = 正向提示词，节点 7 = 负向。
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(_sample_prompt())}
        self.assertEqual(roles[("6", "text")], "positive_prompt")
        self.assertEqual(roles[("7", "text")], "negative_prompt")
        self.assertEqual(roles[("3", "seed")], "", "非提示词槽位没有角色")

    def test_scan_roles_sampler_scheduler_checkpoint(self):
        # 无专用元数据的常用字段按「明确字段名（模型类再叠加节点类名）」识别。
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(_sample_prompt())}
        self.assertEqual(roles[("3", "sampler_name")], "sampler")
        self.assertEqual(roles[("3", "scheduler")], "scheduler")
        self.assertEqual(roles[("4", "ckpt_name")], "checkpoint")
        self.assertEqual(roles[("10", "lora_name")], "lora")
        # 同名文本框但没有对应类名：不误标。
        prompt = _sample_prompt()
        prompt["11"] = {"class_type": "OtherNode", "inputs": {"ckpt_name": "x", "lora_name": "y"}}
        roles2 = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(prompt)}
        self.assertEqual(roles2.get(("11", "ckpt_name")), "")
        self.assertEqual(roles2.get(("11", "lora_name")), "")

    def test_scan_roles_resolution_width_height(self):
        # 出图分辨率也属于常用参数：宽/高标成 resolution，主表单默认显示。
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(_sample_prompt())}
        self.assertEqual(roles[("5", "width")], "resolution")
        self.assertEqual(roles[("5", "height")], "resolution")
        self.assertEqual(roles.get(("5", "batch_size")), "", "批次大小不是分辨率")

    def test_prompt_roles_title_fallback(self):
        prompt = {
            "10": {"class_type": "CLIPTextEncode",
                   "inputs": {"text": "a cat", "clip": ["2", 0]},
                   "_meta": {"title": "负向提示词"}},
            "11": {"class_type": "SomeSamplerNode",
                   "inputs": {"model": ["2", 0], "latent": ["2", 1]}},
            "2": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},
        }
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(prompt)}
        self.assertEqual(roles[("10", "text")], "negative_prompt")

    def test_prompt_roles_no_false_positive(self):
        prompt = {"5": {"class_type": "CLIPTextEncode", "inputs": {"text": "孤立的提示", "clip": ["4", 0]}}}
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(prompt)}
        self.assertEqual(roles[("5", "text")], "", "没有采样器锚点也没有标题线索时不乱标")

    def test_scan_marks_loadimage_filename(self):
        prompt = _sample_prompt()
        prompt["10"] = {"class_type": "LoadImage",
                        "inputs": {"filename": "in.png", "image_upload": True}}
        kinds = {(s["node_id"], s["field_name"]): s["field_type"] for s in scan_params(prompt)}
        self.assertEqual(kinds[("10", "filename")], "image")
        self.assertEqual(kinds[("10", "image_upload")], "bool")

    def test_apply_overrides_preserves_types_and_original(self):
        prompt = _sample_prompt()
        workflow = apply_overrides(prompt, [
            {"node_id": "6", "field_name": "text", "value": "一只狗"},
            {"node_id": "3", "field_name": "steps", "value": "8"},
            {"node_id": "3", "field_name": "cfg", "value": "6.5"},
        ], randomize_seeds=True)
        self.assertEqual(workflow["6"]["inputs"]["text"], "一只狗")
        self.assertEqual(workflow["3"]["inputs"]["steps"], 8)
        self.assertEqual(workflow["3"]["inputs"]["cfg"], 6.5)
        # seed 未覆盖且开启随机：换成了 0..2^50 内的新值；原 JSON 不被改动。
        self.assertNotEqual(workflow["3"]["inputs"]["seed"], 123)
        self.assertLessEqual(workflow["3"]["inputs"]["seed"], 2 ** 50)
        self.assertEqual(prompt["3"]["inputs"]["seed"], 123)
        self.assertEqual(prompt["6"]["inputs"]["text"], "一只猫")
        # 连线与未覆盖槽位原样。
        self.assertEqual(workflow["6"]["inputs"]["clip"], ["4", 1])
        self.assertEqual(workflow["5"]["inputs"]["width"], 512)

    def test_fixed_seed_and_negative_seed(self):
        prompt = _sample_prompt()
        fixed = apply_overrides(prompt, [{"node_id": "3", "field_name": "seed", "value": 42}],
                                randomize_seeds=True)
        self.assertEqual(fixed["3"]["inputs"]["seed"], 42)
        negative = apply_overrides(prompt, [{"node_id": "3", "field_name": "seed", "value": -1}],
                                   randomize_seeds=False)
        self.assertNotEqual(negative["3"]["inputs"]["seed"], -1)


class ServiceImportTests(unittest.TestCase):
    def test_api_format_accepted(self):
        normalized = normalize_api_workflow({"prompt": _sample_prompt()})
        self.assertIn("3", normalized)
        self.assertEqual(normalize_api_workflow(_sample_prompt()), _sample_prompt())

    def test_ui_format_rejected_with_export_guidance(self):
        with self.assertRaises(ValueError) as ctx:
            normalize_api_workflow({"nodes": [], "links": [], "version": "0.4"})
        self.assertIn("导出（API）", str(ctx.exception))
        self.assertEqual(str(ctx.exception), UI_FORMAT_ERROR)

    def test_empty_and_invalid_rejected(self):
        with self.assertRaises(ValueError):
            normalize_api_workflow({})
        with self.assertRaises(ValueError):
            normalize_api_workflow({"1": "not-a-node"})
        with self.assertRaises(ValueError):
            normalize_api_workflow("text")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = ComfyUIStore(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_workflow_crud(self):
        entry = {"id": self.store.new_workflow_id(), "name": "测试",
                 "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
                 "created_at": 1, "updated_at": 1}
        self.store.save_workflow(entry)
        got = self.store.get_workflow(entry["id"])
        self.assertEqual(got["name"], "测试")
        rows = self.store.list_workflows()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("prompt", rows[0], "列表不携带 prompt 本体")
        self.assertEqual(rows[0]["node_count"], len(_sample_prompt()))
        self.assertTrue(self.store.delete_workflow(entry["id"]))
        self.assertIsNone(self.store.get_workflow(entry["id"]))

    def test_workflow_id_traversal_rejected(self):
        self.assertIsNone(self.store.get_workflow("../../etc/passwd"))
        self.assertIsNone(self.store.get_workflow(""))

    def test_task_lifecycle_and_events(self):
        task = self.store.create_task({"workflow_id": "cw-1", "shots": 2, "prompt": {}, "overrides": []})
        task_id = task["id"]
        self.assertTrue(task_id.startswith("cf-"))
        self.store.update_task(task_id, status="running", started_at=1,
                               checkpoint={"prompt_ids": ["p1"], "completed": [], "errors": [],
                                           "resubmits": {}})
        got = self.store.get_task(task_id)
        self.assertEqual(got["status"], "running")
        self.assertEqual(got["checkpoint"]["prompt_ids"], ["p1"])
        # 白名单外的字段不落盘。
        self.store.update_task(task_id, input_data={"hack": True})
        self.assertNotIn("hack", self.store.get_task(task_id))
        seq1 = self.store.append_event(task_id, {"type": "task_started", "message": "x"})
        seq2 = self.store.append_event(task_id, {"type": "shot_submitted", "index": 0})
        self.assertEqual((seq1, seq2), (1, 2))
        rows = self.store.read_events(task_id, after=1)
        self.assertEqual([row["type"] for row in rows], ["shot_submitted"])
        with self.assertRaises(ValueError):
            self.store.append_event(task_id, {"type": "bogus"})

    def test_delete_task_removes_outputs(self):
        task = self.store.create_task({"shots": 1, "prompt": {}, "overrides": []})
        out_dir = self.store.task_output_dir(task["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "001_00_a.png").write_bytes(b"x")
        self.assertTrue(self.store.delete_task(task["id"]))
        self.assertIsNone(self.store.get_task(task["id"]))
        self.assertFalse(out_dir.exists())


class _FakeComfy:
    """假 ComfyUI：可控的提交成功/失败、历史状态、队列、下载字节。"""

    def __init__(self, *, submit_fail: str = "", history_status: str = "success",
                 drop_after_submit: bool = False):
        self.submit_fail = submit_fail
        self.history_status = history_status
        self.drop_after_submit = drop_after_submit
        self.submitted: list[dict] = []
        self.interrupted = 0
        self.deleted_queue: list[str] = []
        self.lock = threading.Lock()

    def patch(self):
        return mock.patch.multiple(
            comfyui_client,
            reachable=mock.DEFAULT, submit=mock.DEFAULT, queue_ids=mock.DEFAULT,
            history_entry=mock.DEFAULT, entry_status=mock.DEFAULT, entry_error=mock.DEFAULT,
            output_items=mock.DEFAULT, download_output=mock.DEFAULT,
            interrupt=mock.DEFAULT, delete_queue_items=mock.DEFAULT,
        )

    def wire(self, patcher):
        patcher["reachable"].side_effect = lambda base, timeout=10: {"version": "test"}
        def _submit(base, workflow):
            if self.submit_fail:
                return "", self.submit_fail
            with self.lock:
                self.submitted.append({"workflow": workflow})
            if self.drop_after_submit:
                return f"pid-{len(self.submitted)}", ""
            return f"pid-{len(self.submitted)}", ""
        patcher["submit"].side_effect = _submit
        patcher["queue_ids"].side_effect = lambda base: set()  # 永不在队列
        patcher["history_entry"].side_effect = self._history
        patcher["entry_status"].side_effect = self._status
        patcher["entry_error"].side_effect = lambda entry: "采样失败：value out of range"
        patcher["output_items"].side_effect = lambda entry: [
            {"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}]
        patcher["download_output"].side_effect = lambda base, item, timeout=120: b"PNG-BYTES"
        patcher["interrupt"].side_effect = lambda base: True
        patcher["delete_queue_items"].side_effect = self._delete_queue

    def _history(self, base, prompt_id):
        if self.drop_after_submit:
            return None
        if self.history_status == "pending":
            return None
        if self.history_status == "error":
            return {"prompt_id": prompt_id, "status": {"status_str": "error", "messages": []},
                    "outputs": {}}
        return {"prompt_id": prompt_id, "status": {"status_str": "success", "messages": []},
                "outputs": {"9": {"images": [{"filename": "ComfyUI_00001_.png",
                                              "subfolder": "", "type": "output"}]}}}

    def _status(self, entry):
        return str((entry.get("status") or {}).get("status_str") or "")

    def _delete_queue(self, base, prompt_ids):
        self.deleted_queue.extend(prompt_ids)
        return True


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = _StubApp(self.dir)
        self.store = ComfyUIStore(self.dir)
        self.runner = ComfyUIRunner(self.app, self.store)

    def tearDown(self):
        self.runner.shutdown(timeout=2)
        self.tmp.cleanup()

    def _make_task(self, shots=2, **kwargs):
        task = self.store.create_task({
            "workflow_id": "cw-1", "workflow_name": "测试", "prompt": _sample_prompt(),
            "overrides": [], "shots": shots, **kwargs,
        })
        return task

    def _run_to_terminal(self, task_id, deadline=15.0):
        end = time.time() + deadline
        while time.time() < end:
            task = self.store.get_task(task_id)
            if task and task["status"] not in ("queued", "running"):
                return task
            time.sleep(0.05)
        raise AssertionError("任务未在期限内结束")

    def test_two_shots_complete_and_outputs_saved(self):
        fake = _FakeComfy()
        task = self._make_task(shots=2)
        with fake.patch() as patcher:
            fake.wire(patcher)
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["progress"], {"done": 2, "total": 2, "failed": 0})
        self.assertEqual(len(fake.submitted), 2)
        outputs = done["result"]["outputs"]
        self.assertEqual(len(outputs), 2)
        for name in outputs:
            path = self.store.task_output_dir(task["id"]) / name
            self.assertEqual(path.read_bytes(), b"PNG-BYTES")
        types = [row["type"] for row in self.store.read_events(task["id"])]
        self.assertIn("task_started", types)
        self.assertEqual(types.count("shot_submitted"), 2)
        self.assertEqual(types.count("shot_done"), 2)
        self.assertIn("task_done", types)

    def test_submit_failure_carries_server_reason(self):
        fake = _FakeComfy(submit_fail="节点 3（KSampler）：seed 超上限")
        task = self._make_task(shots=2)
        with fake.patch() as patcher:
            fake.wire(patcher)
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("第 1 段提交失败", done["error"])
        self.assertIn("seed 超上限", done["error"])
        self.assertEqual(fake.submitted, [], "提交失败后不再提交后续段")

    def test_history_error_status_fails_task(self):
        fake = _FakeComfy(history_status="error")
        task = self._make_task(shots=1)
        with fake.patch() as patcher:
            fake.wire(patcher)
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("第 1 段执行失败", done["error"])

    def test_lost_prompt_resubmits_within_budget_then_fails(self):
        fake = _FakeComfy(drop_after_submit=True)
        task = self._make_task(shots=1)
        with fake.patch() as patcher:
            fake.wire(patcher)
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"], deadline=25.0)
        self.assertEqual(done["status"], "failed")
        self.assertIn("已被 ComfyUI 丢弃", done["error"])
        # 预算 1：初次提交 + 一次重提交 = 2 次。
        self.assertEqual(len(fake.submitted), 2)
        self.assertEqual(done["checkpoint"]["resubmits"].get("0"), 1)

    def test_unreachable_fails_with_actionable_message(self):
        task = self._make_task(shots=1)
        with mock.patch.object(comfyui_client, "reachable", return_value=None):
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("ComfyUI 服务不可达", done["error"])

    def test_cancel_running_calls_interrupt_and_queue_delete(self):
        fake = _FakeComfy(history_status="pending")
        fake.patch()  # 保持 patch 生命周期：手动 enter/exit
        task = self._make_task(shots=1)
        started = threading.Event()
        original_submit = comfyui_client.submit

        def _submit(base, workflow):
            started.set()
            return "pid-1", ""

        with mock.patch.object(comfyui_client, "reachable", return_value={"version": "t"}), \
             mock.patch.object(comfyui_client, "submit", side_effect=_submit), \
             mock.patch.object(comfyui_client, "queue_ids", return_value={"pid-1"}), \
             mock.patch.object(comfyui_client, "history_entry", return_value=None), \
             mock.patch.object(comfyui_client, "interrupt", return_value=True) as mock_int, \
             mock.patch.object(comfyui_client, "delete_queue_items",
                               return_value=True) as mock_del:
            self.runner.submit(task["id"])
            self.assertTrue(started.wait(10))
            # 等 runner 把 prompt_id 写进 checkpoint（取消的两步要对着已登记的段来）。
            deadline = time.time() + 5
            while time.time() < deadline:
                current = self.store.get_task(task["id"]) or {}
                if (current.get("checkpoint") or {}).get("prompt_ids"):
                    break
                time.sleep(0.02)
            self.assertTrue(self.runner.cancel(task["id"], reason="用户取消"))
            done = self._run_to_terminal(task["id"])
            self.assertEqual(done["status"], "cancelled")
            mock_int.assert_called()
            mock_del.assert_called()
        _ = original_submit

    def test_cancel_queued_task_terminals_immediately(self):
        task = self._make_task(shots=1)
        fake = _FakeComfy()
        with fake.patch() as patcher:
            fake.wire(patcher)
            # 不 submit runner：任务停留在 queued，直接取消。
            self.assertTrue(self.runner.cancel(task["id"]))
            done = self.store.get_task(task["id"])
        self.assertEqual(done["status"], "cancelled")

    def test_resume_keeps_completed_segments(self):
        fake = _FakeComfy()
        task = self._make_task(shots=2)
        # 预置 checkpoint：第 0 段已完成（含产物文件），第 1 段待跑。
        out_dir = self.store.task_output_dir(task["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "001_00_old.png").write_bytes(b"old")
        self.store.update_task(task["id"], status="queued", checkpoint={
            "prompt_ids": ["pid-old", ""],
            "completed": [{"index": 0, "prompt_id": "pid-old", "files": ["001_00_old.png"]}],
            "errors": [], "resubmits": {},
        })
        with fake.patch() as patcher:
            fake.wire(patcher)
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        # 只提交了未完成的第 1 段；第 0 段产物保留。
        self.assertEqual(len(fake.submitted), 1)
        self.assertEqual(done["result"]["outputs"], ["001_00_old.png", "002_00_ComfyUI_00001_.png"])

    def test_recover_interrupted_marks_active_tasks(self):
        task = self._make_task(shots=3)
        self.store.update_task(task["id"], status="running")
        manager = ComfyUIJobManager(self.app, self.store, self.runner)
        recovered = manager.recover_interrupted()
        self.assertEqual(recovered, 1)
        got = self.store.get_task(task["id"])
        self.assertEqual(got["status"], "interrupted")
        self.assertIn("重试", got["detail"])
        # 重启恢复成终态时要补上结束时刻，不能沿用启动前的旧值或缺失。
        self.assertIsNotNone(got["finished_at"])


class _StubComfyRunner:
    """JobsTests 用：任务管理层的桩 runner——不启动任何后台线程。

    真 runner 会在提交后立刻起线程跑任务（reach 不到 ComfyUI 就终态化），
    与测试里手写的 update_task 产生竞态；任务管理层测试只需要 submit 被调用。
    """

    def __init__(self, store: ComfyUIStore | None = None):
        self.submitted: list[str] = []
        self.store = store

    def submit(self, task_id: str) -> None:
        self.submitted.append(str(task_id))

    def cancel(self, task_id: str, *, reason: str = "") -> bool:
        # 模拟真 runner 对**排队**任务的取消：同步本地终态化（运行中任务则由
        # worker 收尾，测试用显式的状态翻转桩另行覆盖）。
        if self.store is not None:
            self.store.update_task(task_id, status="cancelled")
        return True

    def shutdown(self, timeout: float = 0.0) -> None:
        pass


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = _StubApp(self.dir)
        self.store = ComfyUIStore(self.dir)
        self.runner = _StubComfyRunner()
        self.jobs = ComfyUIJobManager(self.app, self.store, self.runner)
        self.entry = self.store.save_workflow({
            "id": self.store.new_workflow_id(), "name": "测试工作流",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "created_at": 1, "updated_at": 1,
        })

    def tearDown(self):
        self.runner.shutdown()
        self.tmp.cleanup()

    def test_submit_requires_known_workflow_and_slots(self):
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({})
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({"workflow_id": "cw-missing"})
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({
                "workflow_id": self.entry["id"],
                "overrides": [{"node_id": "99", "field_name": "nope", "value": 1}],
            })
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({"workflow_id": self.entry["id"], "shots": "abc"})

    def test_submit_validates_and_converts_overrides(self):
        task = self.jobs.submit({
            "workflow_id": self.entry["id"], "shots": "2",
            "overrides": [
                {"node_id": "6", "field_name": "text", "value": "一只狗"},
                {"node_id": "3", "field_name": "steps", "value": "9"},
                {"node_id": "3", "field_name": "seed", "value": ""},
            ],
        })
        self.assertEqual(len(task["input"]["overrides"]), 2, "空 seed = 随机，不进 overrides")
        self.assertEqual(task["input"]["overrides"][1]["value"], 9)
        self.assertEqual(task["input"]["prompt"], _sample_prompt())
        self.assertEqual(task["input"]["shots"], 2)
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({
                "workflow_id": self.entry["id"],
                "overrides": [{"node_id": "3", "field_name": "steps", "value": "abc"}],
            })

    def test_retry_keeps_completed_and_clears_errors(self):
        task = self.jobs.submit({"workflow_id": self.entry["id"], "shots": 3})
        task_id = task["id"]
        self.store.update_task(task_id, status="failed", checkpoint={
            "prompt_ids": ["p1", "p2", ""],
            "completed": [{"index": 0, "prompt_id": "p1", "files": ["a.png"]}],
            "errors": [{"index": 1, "prompt_id": "p2", "error": "boom"}],
            "resubmits": {},
        })
        retried = self.jobs.retry(task_id)
        self.assertEqual(retried["status"], "queued")
        checkpoint = retried["checkpoint"]
        self.assertEqual(len(checkpoint["completed"]), 1)
        self.assertEqual(checkpoint["errors"], [])
        # 全部完成后再重试：明确拒绝。
        self.store.update_task(task_id, status="failed", checkpoint={
            "prompt_ids": ["p1", "p2", "p3"],
            "completed": [{"index": i, "prompt_id": f"p{i + 1}", "files": []} for i in range(3)],
            "errors": [], "resubmits": {},
        })
        with self.assertRaises(ComfyUITaskError):
            self.jobs.retry(task_id)

    def test_retry_active_task_rejected(self):
        task = self.jobs.submit({"workflow_id": self.entry["id"]})
        self.store.update_task(task["id"], status="running")
        with self.assertRaises(ComfyUITaskError):
            self.jobs.retry(task["id"])

    def test_retry_clears_finished_at(self):
        # 重试是新的一轮运行：上一轮的结束时刻必须清掉，不能沿用旧值。
        task = self.jobs.submit({"workflow_id": self.entry["id"], "shots": 2})
        self.store.update_task(task["id"], status="failed", finished_at=12345)
        retried = self.jobs.retry(task["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertIsNone(retried["finished_at"])

    def test_delete_tasks_skips_active_and_removes_outputs(self):
        # 直接落任务记录（不经过 runner：真 runner 会把任务推到终态，干扰断言）。
        finished = self.store.create_task({"workflow_id": "cw-x", "shots": 1, "prompt": {}, "overrides": []})
        running = self.store.create_task({"workflow_id": "cw-x", "shots": 1, "prompt": {}, "overrides": []})
        self.store.update_task(finished["id"], status="completed")
        self.store.update_task(running["id"], status="running")
        out_dir = self.store.task_output_dir(finished["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "x.png").write_bytes(b"x")
        result = self.jobs.delete_tasks({})
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["skipped"], [running["id"]])
        self.assertEqual(result["failed"], [])
        self.assertFalse(out_dir.exists())
        self.assertIsNotNone(self.store.get_task(running["id"]))

    def test_delete_tasks_cancels_active_then_deletes(self):
        # 计划 §2：删除排队/运行中的任务 = 先取消、等终态、再删记录与产物。
        task = self.store.create_task({"workflow_id": "cw-x", "shots": 1, "prompt": {}, "overrides": []})
        self.store.update_task(task["id"], status="running")
        out_dir = self.store.task_output_dir(task["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "x.png").write_bytes(b"x")
        runner = _StubComfyRunner(self.store)
        jobs = ComfyUIJobManager(self.app, self.store, runner)
        result = jobs.delete_tasks(
            {"task_ids": [task["id"]], "cancel_active": True}, cancel_timeout=5.0)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["failed"], [])
        self.assertEqual(runner.submitted, [])
        self.assertIsNone(self.store.get_task(task["id"]))
        self.assertFalse(out_dir.exists(), "取消后的删除要清掉产物目录")

    def test_delete_tasks_timeout_keeps_task_and_reports_reason(self):
        # 取消发出了但任务迟迟不停（模拟运行中任务还没收尾）：如实回报失败原因，
        # 任务保留，不伪装成已删除。
        task = self.store.create_task({"workflow_id": "cw-x", "shots": 1, "prompt": {}, "overrides": []})
        self.store.update_task(task["id"], status="running")
        result = self.jobs.delete_tasks(
            {"task_ids": [task["id"]], "cancel_active": True}, cancel_timeout=0.3)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["id"], task["id"])
        self.assertTrue(result["failed"][0]["reason"])
        self.assertIsNotNone(self.store.get_task(task["id"]))

    def test_delete_tasks_cancels_queued_task_immediately(self):
        # 排队中的任务取消是同步终态化：无需等待窗口。
        task = self.store.create_task({"workflow_id": "cw-x", "shots": 1, "prompt": {}, "overrides": []})
        jobs = ComfyUIJobManager(self.app, self.store, _StubComfyRunner(self.store))
        result = jobs.delete_tasks(
            {"task_ids": [task["id"]], "cancel_active": True}, cancel_timeout=5.0)
        self.assertEqual(result["deleted"], 1)
        self.assertIsNone(self.store.get_task(task["id"]))

    def test_delete_workflow_blocked_by_active_task(self):
        task = self.store.create_task({"workflow_id": self.entry["id"], "shots": 1,
                                       "prompt": {}, "overrides": []})
        self.store.update_task(task["id"], status="running")
        ok, reason = self.jobs.delete_workflow(self.entry["id"])
        self.assertFalse(ok)
        self.assertIn("取消", reason)
        # 任务终态后可删。
        self.store.update_task(task["id"], status="completed")
        ok, reason = self.jobs.delete_workflow(self.entry["id"])
        self.assertTrue(ok)


class ServiceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = _StubApp(self.dir)
        self.service = ComfyUIService(self.app)

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def test_import_from_json_string_and_detail(self):
        payload, status = self.service.api_workflow_create({
            "name": "粘贴导入", "workflow": json.dumps({"prompt": _sample_prompt()}),
        })
        self.assertEqual(status, 201)
        workflow_id = payload["workflow"]["id"]
        detail, status2 = self.service.api_workflow(workflow_id)
        self.assertEqual(status2, 200)
        self.assertEqual(detail["workflow"]["prompt"], _sample_prompt())
        self.assertTrue(any(s["field_type"] == "seed" for s in detail["workflow"]["params"]))

    def test_detail_upgrades_legacy_params_with_roles(self):
        # 模拟旧版导入：params 没有 role 字段 → 详情时懒升级补上并落盘。
        entry = {
            "id": self.service.store.new_workflow_id(), "name": "旧导入",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "created_at": 1, "updated_at": 1,
        }
        for slot in entry["params"]:
            slot.pop("role", None)
        self.service.store.save_workflow(entry)
        detail, _status = self.service.api_workflow(entry["id"])
        roles = {(s["node_id"], s["field_name"]): s.get("role")
                 for s in detail["workflow"]["params"]}
        self.assertEqual(roles[("6", "text")], "positive_prompt")
        # 升级结果已落盘：再取一次直接带 role（不再依赖重扫）。
        again = self.service.store.get_workflow(entry["id"])
        self.assertTrue(any(s.get("role") == "positive_prompt" for s in again["params"]))
        self.assertEqual(again.get("params_version"), PARAMS_SCHEMA_VERSION)

    def test_detail_upgrades_v1_params_to_new_roles(self):
        # v1 时代已升级过 prompt role 的工作流：schema 版本推进后仍要重扫一次，
        # 补上后加的 sampler/scheduler/checkpoint 角色，且升级结果落盘。
        entry = {
            "id": self.service.store.new_workflow_id(), "name": "v1 导入",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "created_at": 1, "updated_at": 1,
        }
        for slot in entry["params"]:
            if slot.get("role") in ("sampler", "scheduler", "checkpoint"):
                slot["role"] = ""
        self.service.store.save_workflow(entry)
        detail, _status = self.service.api_workflow(entry["id"])
        roles = {(s["node_id"], s["field_name"]): s.get("role")
                 for s in detail["workflow"]["params"]}
        self.assertEqual(roles[("3", "sampler_name")], "sampler")
        self.assertEqual(roles[("4", "ckpt_name")], "checkpoint")
        again = self.service.store.get_workflow(entry["id"])
        self.assertEqual(again.get("params_version"), PARAMS_SCHEMA_VERSION)

    def test_workflow_visible_params_saved_and_restored(self):
        entry = self.service.store.save_workflow({
            "id": self.service.store.new_workflow_id(), "name": "n",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": 1, "updated_at": 1,
        })
        visible = [{"node_id": "6", "field_name": "text"},
                   {"node_id": "3", "field_name": "seed"}]
        payload, status = self.service.api_workflow_params_set(entry["id"], {"visible": visible})
        self.assertEqual(status, 200)
        self.assertEqual(payload["visible_params"], visible)
        # 按工作流持久化：重新读取详情能恢复，别的字段不受影响。
        detail, _status = self.service.api_workflow(entry["id"])
        self.assertEqual(detail["workflow"]["visible_params"], visible)

    def test_workflow_visible_params_rejects_invalid(self):
        entry = self.service.store.save_workflow({
            "id": self.service.store.new_workflow_id(), "name": "n",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": 1, "updated_at": 1,
        })
        for bad in ({"visible": "nope"},
                    {"visible": [{"node_id": "99", "field_name": "x"}]},
                    {"visible": ["not-an-object"]}):
            payload, status = self.service.api_workflow_params_set(entry["id"], bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("error", payload)
        self.assertNotIn("visible_params", self.service.store.get_workflow(entry["id"]))

    def test_api_models_unreachable_returns_empty_with_error(self):
        # ComfyUI 不可达：空候选 + 说明；不阻断工作流编辑与提交（前端有自由输入）。
        with mock.patch.object(comfyui_client, "checkpoint_names", return_value=[]), \
             mock.patch.object(comfyui_client, "lora_names", return_value=[]):
            payload, status = self.service.api_models()
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["models"], [])
        self.assertEqual(payload["loras"], [])
        self.assertIn("手动输入", payload["error"])

    def test_api_models_returns_candidates(self):
        with mock.patch.object(comfyui_client, "checkpoint_names",
                               return_value=["a.safetensors", "b.safetensors"]), \
             mock.patch.object(comfyui_client, "lora_names", return_value=["l.safetensors"]):
            payload, _status = self.service.api_models()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["models"], ["a.safetensors", "b.safetensors"])
        self.assertEqual(payload["loras"], ["l.safetensors"])

    def test_api_models_ok_when_only_loras(self):
        # 有 LoRA 没 checkpoint 也算候选可用（ok 只看两者任一非空）。
        with mock.patch.object(comfyui_client, "checkpoint_names", return_value=[]), \
             mock.patch.object(comfyui_client, "lora_names", return_value=["l.safetensors"]):
            payload, _status = self.service.api_models()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["models"], [])
        self.assertEqual(payload["loras"], ["l.safetensors"])

    def test_tasks_delete_cancels_active_when_requested(self):
        # 门面集成：cancel_active 走真 runner——排队任务直接终态化后删除。
        self.service.store.save_workflow({
            "id": "cw-del", "name": "n", "prompt": _sample_prompt(), "params": [],
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": 1, "updated_at": 1,
        })
        task = self.service.store.create_task({"workflow_id": "cw-del", "shots": 1,
                                               "prompt": {}, "overrides": []})
        payload, status = self.service.api_tasks_delete({
            "task_ids": [task["id"]], "cancel_active": True, "cancel_timeout": 5,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["deleted"], 1)
        self.assertIsNone(self.service.store.get_task(task["id"]))

    def test_health_reports_unreachable(self):
        payload, status = self.service.api_health()
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("ComfyUI", payload["error"])

    def test_set_url_saves_and_reports_reachability(self):
        # 可达（打桩）：地址入库并返回 reachable=True。
        with mock.patch.object(comfyui_client, "reachable", return_value={"version": "v1"}):
            payload, status = self.service.api_set_url({"url": "127.0.0.1:8188/"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["reachable"])
        self.assertEqual(payload["url"], "http://127.0.0.1:8188")
        self.assertEqual(self.app.config.data["comfyui_url"], "http://127.0.0.1:8188")
        # 不可达：保存成功但如实告知连不上。
        with mock.patch.object(comfyui_client, "reachable", return_value=None):
            payload2, status2 = self.service.api_set_url({"url": "http://127.0.0.1:9"})
        self.assertEqual(status2, 200)
        self.assertFalse(payload2["reachable"])
        self.assertIn("连不上", payload2["error"])

    def test_set_url_rejects_bad_format(self):
        original = self.app.config.data.get("comfyui_url")
        for bad in ("", "   ", "ftp://x", "http://"):
            payload, status = self.service.api_set_url({"url": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("http", payload["error"])
        self.assertEqual(self.app.config.data.get("comfyui_url"), original,
                         "非法地址不能写进配置")

    def test_set_url_keeps_path_prefix(self):
        with mock.patch.object(comfyui_client, "reachable", return_value=None):
            payload, _status = self.service.api_set_url({"url": "http://192.168.1.5:8188/comfy/"})
        self.assertEqual(payload["url"], "http://192.168.1.5:8188/comfy")
        self.assertEqual(self.app.config.data["comfyui_url"], "http://192.168.1.5:8188/comfy")

    def test_task_file_rejects_traversal_and_missing(self):
        task = self.service.store.create_task({"shots": 1, "prompt": {}, "overrides": []})
        out_dir = self.service.store.task_output_dir(task["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "ok.png").write_bytes(b"x")
        self.assertIsNotNone(self.service.task_file(task["id"], "ok.png"))
        self.assertIsNone(self.service.task_file(task["id"], "../secret.png"))
        self.assertIsNone(self.service.task_file(task["id"], "a/b.png"))
        self.assertIsNone(self.service.task_file(task["id"], ".hidden"))
        self.assertIsNone(self.service.task_file(task["id"], "missing.png"))
        self.assertIsNone(self.service.task_file("cf-none", "ok.png"))

    def test_slim_tasks_hide_checkpoint(self):
        self.service.store.save_workflow({
            "id": "cw-x", "name": "n", "prompt": _sample_prompt(), "params": [],
            "created_at": 1, "updated_at": 1,
        })
        self.service.api_submit({"workflow_id": "cw-x", "shots": 1})
        rows, _ = self.service.api_tasks()
        self.assertEqual(len(rows["tasks"]), 1)
        self.assertNotIn("checkpoint", rows["tasks"][0])
        self.assertIn("workflow_name", rows["tasks"][0])

    def test_slim_tasks_include_output_file_paths(self):
        # 前端要拿绝对路径去走 /api/file：手机/局域网靠 URL 口令取字节，
        # 桌面靠 path 参数弹原生「另存为」——只回文件名两条链路都断了。
        task = self.service.store.create_task({"shots": 1, "prompt": {}, "overrides": []})
        out_dir = self.service.store.task_output_dir(task["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "a.png").write_bytes(b"x")
        self.service.store.update_task(
            task["id"], status="completed",
            result={"completed": [], "outputs": ["a.png"]},
        )
        rows, _ = self.service.api_tasks()
        row = next(item for item in rows["tasks"] if item["id"] == task["id"])
        self.assertEqual(
            row["output_files"],
            [{"name": "a.png", "path": str(out_dir / "a.png")}],
        )


if __name__ == "__main__":
    unittest.main()
