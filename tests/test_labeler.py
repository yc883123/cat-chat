# -*- coding: utf-8 -*-
"""护栏：打标器（docs/plans/2026-10-23-打标器接入工作台计划.md）。

覆盖六条主线：
1. 输入安全：只认受管 uploads、扩展名/解码校验、批量上限、重复去重；
2. 配置契约：白名单、非在线拒绝、未验证并发拒绝、config_fingerprint 稳定且排除易变项；
3. 逐图事务：标注 + 状态 + 用量一次提交；running 崩溃 → uncertain；
4. 执行与恢复：复用既有结果不重复调用、瞬时错误重试、永久错误不重试、取消语义、
   启动恢复只把 running 判为 uncertain；
5. 「继续未完成」：新建关联任务、成功项不重复、uncertain 必须显式确认；
6. 导出与句子库：三格式、CSV 公式注入防护、受管落点、degraded 标记。
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core.paths import normalized_path_key  # noqa: E402
from naiba.labeler import LABELER_ITEM_STATES  # noqa: E402
from naiba.labeler.db import LabelerDB  # noqa: E402
from naiba.labeler.engines.provider import parse_response  # noqa: E402
from naiba.labeler.export import LabelerExportError, render, write_export  # noqa: E402
from naiba.labeler.images import (  # noqa: E402
    MAX_BATCH_IMAGES,
    LabelerImageError,
    stable_item_id,
    validate_image_refs,
)
from naiba.labeler.jobs import (  # noqa: E402
    LabelerJobManager,
    LabelerTaskError,
    config_fingerprint,
    normalize_config,
)
from naiba.labeler.presets import DEFAULT_PRESET, PRESET_IDS, filter_tags, get_preset, preset_list  # noqa: E402
from naiba.labeler.runner import LabelerRunner, aggregate_usage, classify_error  # noqa: E402
from naiba.labeler.sentences import sentence_stats, split_sentences  # noqa: E402
from naiba.labeler.store import LabelerStore  # noqa: E402


def _write_png(path: Path, color=(20, 120, 200), size=(16, 16)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


class _StubConfig:
    def __init__(self, kind: str = "online"):
        self.kind = kind

    def profile(self, key):
        return {"kind": self.kind, "name": "stub", "model": "stub-model",
                "base_url": "http://127.0.0.1:9"}

    def runtime_guard_options(self):
        return {}


class _StubModels:
    def __init__(self, behavior=None):
        self.behavior = behavior or (
            lambda profile, messages, options, status: '{"tags":[{"tag":"cat","confidence":0.9}],'
                                                       '"description":"一只猫"}'
        )
        self.calls: list[dict] = []
        self.last_usage: dict = {}
        self.last_reasoning = ""
        self.last_finish_reason = ""

    def complete(self, profile, messages, options, status=None):
        self.calls.append({"messages": messages, "options": options})
        text = self.behavior(profile, messages, options, status)
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        self.last_finish_reason = "stop"
        return text


class _StubPaths:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)


class _StubRuns:
    """模拟「有对话在跑」：前 N 次查询返回活跃，之后返回空。"""

    def __init__(self, active_times: int = 0):
        self.active_times = active_times
        self.calls = 0

    def list_primary(self, conversation_id="", active_only=False):
        self.calls += 1
        if self.active_times > 0:
            self.active_times -= 1
            return [{"id": "run-active"}]
        return []


class _StubApp:
    def __init__(self, data_dir: Path, *, models=None, config=None):
        self.paths = _StubPaths(data_dir)
        self.models = models or _StubModels()
        self.config = config or _StubConfig()
        self.runs = None


def _base_config(**overrides):
    config = {
        "engine": "provider",
        "output_mode": "keywords",
        "preset": DEFAULT_PRESET,
        "model_key": "online:stub",
        "model_name": "stub-model",
        "confidence_threshold": 0.35,
        "max_tags": 24,
        "custom_prompt": "",
        "subject_filter": {"enabled": False, "keywords": [], "strength": "medium"},
        "concurrency": 1,
        "retry_limit": 1,
        "timeout_seconds": 90,
        "prompt_version": 1,
    }
    config.update(overrides)
    return config


class LabelerImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.uploads = self.data_dir / "uploads"
        self.upload = _write_png(self.uploads / "2026-10-23" / "cover.png")

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_managed_uploads_and_keeps_relative_path(self):
        refs = validate_image_refs(self.data_dir, [{
            "path": str(self.upload), "name": "cover.png",
            "relative_path": "chapter-01/cover.png",
        }])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["relative_path"], "chapter-01/cover.png")
        # 校验端会 resolve()（短路径 TEMP 下会展开 junction），比较必须同口径。
        self.assertEqual(refs[0]["path_key"], normalized_path_key(self.upload.resolve()))
        self.assertIn(":", refs[0]["content_version"])

    def test_rejects_path_outside_uploads(self):
        outside = _write_png(self.data_dir / "elsewhere" / "x.png")
        with self.assertRaises(LabelerImageError):
            validate_image_refs(self.data_dir, [{"path": str(outside)}])

    def test_rejects_non_image_extension_and_bad_bytes(self):
        bad_ext = self.uploads / "note.txt"
        bad_ext.write_text("hi", encoding="utf-8")
        with self.assertRaises(LabelerImageError):
            validate_image_refs(self.data_dir, [{"path": str(bad_ext)}])
        fake = self.uploads / "fake.png"
        fake.write_bytes(b"not an image")
        with self.assertRaises(LabelerImageError):
            validate_image_refs(self.data_dir, [{"path": str(fake)}])

    def test_rejects_too_many_images(self):
        refs = [{"path": str(self.upload)} for _ in range(MAX_BATCH_IMAGES + 1)]
        with self.assertRaises(LabelerImageError):
            validate_image_refs(self.data_dir, refs)

    def test_sanitizes_relative_path_traversal(self):
        refs = validate_image_refs(self.data_dir, [{
            "path": str(self.upload), "relative_path": "..\\..\\windows\\x.png",
        }])
        # 只保留相对名称段，绝不带 .. 或盘符。
        self.assertNotIn("..", refs[0]["relative_path"])
        self.assertNotIn(":", refs[0]["relative_path"])

    def test_dedupes_same_file_in_one_batch(self):
        refs = validate_image_refs(self.data_dir, [
            {"path": str(self.upload)}, {"path": str(self.upload)},
        ])
        self.assertEqual(len(refs), 1)

    def test_stable_item_id_changes_with_content_version(self):
        a = stable_item_id("tg-1", "k", "1:1")
        b = stable_item_id("tg-1", "k", "1:2")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("it-"))


class LabelerConfigTests(unittest.TestCase):
    def setUp(self):
        self.app = _StubApp(Path(tempfile.gettempdir()))

    def test_rejects_unknown_key_and_secret_fields(self):
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(api_key="sk-should-not-be-accepted"))
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(provider_info={"base_url": "http://evil"}))

    def test_accepts_local_profile_but_keeps_concurrency_one(self):
        # 用户口径：有 provider 就行，本地后端也算 provider；但并发仍固定 1。
        app = _StubApp(Path(tempfile.gettempdir()), config=_StubConfig(kind="local"))
        config = normalize_config(app, _base_config())
        self.assertEqual(config["concurrency"], 1)
        self.assertEqual(config["engine"], "provider")

    def test_rejects_unvalidated_concurrency(self):
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(concurrency=4))

    def test_fingerprint_ignores_volatile_fields(self):
        a = normalize_config(self.app, _base_config(retry_limit=0, timeout_seconds=30, concurrency=1))
        b = normalize_config(self.app, _base_config(retry_limit=5, timeout_seconds=600, concurrency=1))
        self.assertEqual(a["config_fingerprint"], b["config_fingerprint"])
        c = normalize_config(self.app, _base_config(preset="style_lora"))
        self.assertNotEqual(a["config_fingerprint"], c["config_fingerprint"])

    def test_preset_ids_are_all_known(self):
        self.assertEqual(len(set(PRESET_IDS)), 6)
        self.assertEqual(len(preset_list()), 6)
        with self.assertRaises(ValueError):
            get_preset("nope")


class LabelerPresetAndParseTests(unittest.TestCase):
    def test_filter_drops_noise_and_duplicates(self):
        preset = get_preset("style_lora")
        tags = ["Oil painting", "oil painting", "high quality", "watercolor", "nice"]
        out = filter_tags(tags, preset=preset)
        self.assertEqual(out, ["oil painting", "watercolor"])

    def test_subject_filter_extra_keywords(self):
        out = filter_tags(["cat", "dog", "person"], preset=get_preset("object_lora"),
                          extra_keywords=["dog"], strength="medium")
        self.assertNotIn("dog", out)

    def test_parse_json_with_code_fence(self):
        parsed = parse_response('```json\n{"tags":[{"tag":"cat","confidence":0.9}],"description":"d"}\n```')
        self.assertEqual(parsed["tags"], ["cat"])
        self.assertEqual(parsed["confidences"], [0.9])
        self.assertEqual(parsed["description"], "d")
        self.assertEqual(parsed["parsed"], "json")

    def test_parse_fallback_comma_list(self):
        parsed = parse_response("cat, dog, warm light")
        self.assertEqual(parsed["tags"], ["cat", "dog", "warm light"])
        self.assertEqual(parsed["parsed"], "fallback")

    def test_classify_error(self):
        self.assertEqual(classify_error(RuntimeError("HTTP 429 rate limit")), "transient")
        self.assertEqual(classify_error(RuntimeError("401 unauthorized")), "auth")
        self.assertEqual(classify_error(RuntimeError("400 bad request")), "bad_request")
        self.assertEqual(classify_error(RuntimeError("任务已取消")), "cancelled")
        self.assertEqual(classify_error(RuntimeError("weird thing")), "permanent")


class LabelerDBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.db = LabelerDB(self.data_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _item(self, item_id="it-1", key="k1", version="1:1"):
        return {
            "item_id": item_id, "path": "C:/data/uploads/a.png", "path_key": key,
            "content_version": version, "relative_path": "a.png", "name": "a.png",
            "size": 10, "mtime_ns": 1,
        }

    def _annotation(self, key="k1", version="1:1", tags=None):
        return {
            "path_key": key, "content_version": version, "path": "C:/data/uploads/a.png",
            "relative_path": "a.png", "name": "a.png", "engine": "provider",
            "output_mode": "keywords", "preset": "face_lora", "prompt_version": 1,
            "model_key": "online:stub", "model_name": "stub-model",
            "tags": tags or ["cat"], "confidences": [0.9], "description": "",
        }

    def test_complete_item_is_one_transaction_and_counts(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.assertEqual(self.db.counts("tg-1")["queued"], 1)
        self.db.mark_running("tg-1", "it-1")
        self.assertEqual(self.db.counts("tg-1")["running"], 1)
        annotation_id, reused = self.db.complete_item(
            "tg-1", "it-1", annotation=self._annotation(), usage={"total_tokens": 15},
            config_fingerprint="cfg-a",
        )
        self.assertFalse(reused)
        self.assertGreater(annotation_id, 0)
        counts = self.db.counts("tg-1")
        self.assertEqual(counts["done"], 1)
        self.assertEqual(counts["running"], 0)
        self.assertEqual(self.db.annotation_ids_for_task("tg-1"), [annotation_id])
        self.assertEqual(self.db.list_items("tg-1")[0]["usage"], {"total_tokens": 15})

    def test_unique_identity_reuses_row(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        first, _ = self.db.complete_item("tg-1", "it-1", annotation=self._annotation(),
                                        usage={}, config_fingerprint="cfg-a")
        reused_row = self.db.find_reusable("k1", "1:1", "cfg-a")
        self.assertIsNotNone(reused_row)
        self.assertEqual(reused_row["id"], first)
        second, reused = self.db.complete_item(
            "tg-1", "it-1", annotation=self._annotation(tags=["cat", "fur"]),
            usage={}, config_fingerprint="cfg-a",
        )
        self.assertTrue(reused)
        self.assertEqual(first, second)
        self.assertEqual(self.db.find_reusable("k1", "1:1", "cfg-a")["tags"], ["cat", "fur"])

    def test_reuse_keeps_original_task_ownership_but_join_sees_both(self):
        """复用不搬走历史：标注仍归首个任务，后续任务通过 task_items 关联可见。"""
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.db.complete_item("tg-1", "it-1", annotation=self._annotation(),
                              usage={}, config_fingerprint="cfg-a")
        self.db.add_items("tg-2", [self._item()], "cfg-a")
        self.db.complete_item("tg-2", "it-1", annotation=self._annotation(),
                              usage={}, config_fingerprint="cfg-a")
        row = self.db.annotations(limit=10)[0]
        self.assertEqual(row["source_task_id"], "tg-1", "归属不能被重跑改写")
        self.assertEqual(len(self.db.annotations(task_id="tg-1")), 1)
        self.assertEqual(len(self.db.annotations(task_id="tg-2")), 1)

    def test_different_fingerprint_keeps_history(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.db.complete_item("tg-1", "it-1", annotation=self._annotation(),
                             usage={}, config_fingerprint="cfg-a")
        self.db.add_items("tg-2", [self._item()], "cfg-b")
        self.db.complete_item("tg-2", "it-1", annotation=self._annotation(),
                             usage={}, config_fingerprint="cfg-b")
        rows = self.db.annotations(limit=10)
        self.assertEqual(len(rows), 2)

    def test_running_becomes_uncertain_on_recovery(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.db.mark_running("tg-1", "it-1")
        self.assertEqual(self.db.mark_running_as_uncertain(), 1)
        counts = self.db.counts("tg-1")
        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual(counts["running"], 0)

    def test_failed_item_keeps_usage_and_category(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.db.fail_item("tg-1", "it-1", error="HTTP 500", category="transient",
                          usage={"total_tokens": 3})
        item = self.db.list_items("tg-1")[0]
        self.assertEqual(item["state"], "failed")
        self.assertEqual(item["error_category"], "transient")
        self.assertEqual(item["usage"], {"total_tokens": 3})

    def test_all_item_states_are_declared(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.assertIn(self.db.list_items("tg-1")[0]["state"], LABELER_ITEM_STATES)

    def test_referenced_paths_only_for_given_tasks(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.assertEqual(self.db.referenced_paths(["tg-1"]), {"C:/data/uploads/a.png"})
        self.assertEqual(self.db.referenced_paths(["tg-9"]), set())


class LabelerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 清理顺序很重要：runner 必须在删临时目录之前停掉，否则 Windows 上
        # 还在跑的 worker 会占着 labeler.db，tearDown 报 PermissionError。
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.uploads = self.data_dir / "uploads"
        self.images = [
            _write_png(self.uploads / "d" / f"a{i}.png", color=(10 * i, 100, 200))
            for i in range(3)
        ]
        self.store = LabelerStore(self.data_dir)
        self.db = LabelerDB(self.data_dir)

    def _make_app(self, models=None):
        return _StubApp(self.data_dir, models=models or _StubModels())

    def _jobs(self, app):
        runner = LabelerRunner(app, self.store, self.db)
        self.addCleanup(runner.shutdown)
        return LabelerJobManager(app, self.store, self.db, runner)

    def _submit(self, app, config=None, images=None):
        jobs = self._jobs(app)
        refs = [
            {"path": str(path), "name": path.name, "relative_path": f"d/{path.name}"}
            for path in (images if images is not None else self.images)
        ]
        task = jobs.submit({"image_refs": refs, "config": config or _base_config()})
        return jobs, task

    def _wait(self, task_id, timeout=10.0):
        deadline = time.time() + timeout
        state = None
        while time.time() < deadline:
            state = self.store.get_task(task_id)
            if state and state.get("status") in {"done", "failed", "cancelled", "interrupted"}:
                return state
            time.sleep(0.02)
        return state

    def test_success_path_records_every_item_and_usage(self):
        app = self._make_app()
        _, task = self._submit(app)
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        counts = self.db.counts(task["id"])
        self.assertEqual(counts["done"], 3)
        self.assertEqual(state["usage"].get("total_tokens"), 45)
        self.assertEqual(len(state["result"]["annotation_ids"]), 3)
        self.assertEqual(len(app.models.calls), 3)
        # 图片以多模态 part 发送（不是把路径拼进提示文本）。
        first_content = app.models.calls[0]["messages"][0]["content"]
        self.assertEqual(first_content[0]["type"], "image")

    def test_reuses_existing_result_without_calling_model(self):
        app = self._make_app()
        _, task = self._submit(app, images=self.images[:1])
        self._wait(task["id"])
        calls_before = len(app.models.calls)
        _, task2 = self._submit(app, images=self.images[:1])
        state = self._wait(task2["id"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(len(app.models.calls), calls_before, "同图同配置不应重复调用模型")
        self.assertEqual(self.db.counts(task2["id"])["done"], 1)

    def test_transient_error_retries_then_succeeds(self):
        remaining = {"n": 1}

        def behavior(profile, messages, options, status):
            if remaining["n"] > 0:
                remaining["n"] -= 1
                raise RuntimeError("HTTP 503 service unavailable")
            return '{"tags":[{"tag":"cat","confidence":0.9}],"description":""}'

        app = self._make_app(_StubModels(behavior))
        _, task = self._submit(app, config=_base_config(retry_limit=1), images=self.images[:1])
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(self.db.counts(task["id"])["done"], 1)
        self.assertEqual(len(app.models.calls), 2)

    def test_permanent_error_not_retried(self):
        def behavior(profile, messages, options, status):
            raise RuntimeError("401 unauthorized: invalid api key")

        app = self._make_app(_StubModels(behavior))
        _, task = self._submit(app, config=_base_config(retry_limit=3), images=self.images[:1])
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(len(app.models.calls), 1)
        item = self.db.list_items(task["id"])[0]
        self.assertEqual(item["state"], "failed")
        self.assertEqual(item["error_category"], "auth")

    def test_cancel_running_task_stops_and_marks_remaining(self):
        started = threading.Event()

        def behavior(profile, messages, options, status):
            started.set()
            for _ in range(300):
                if options["cancel_event"].is_set():
                    raise RuntimeError("任务已取消")
                time.sleep(0.01)
            return '{"tags":[{"tag":"cat","confidence":0.9}],"description":""}'

        app = self._make_app(_StubModels(behavior))
        jobs, task = self._submit(app)
        self.assertTrue(started.wait(5))
        self.assertTrue(jobs.cancel(task["id"]))
        state = self._wait(task["id"], timeout=15)
        self.assertEqual(state["status"], "cancelled")
        counts = self.db.counts(task["id"])
        self.assertEqual(counts["cancelled"], 3)
        self.assertEqual(counts["running"], 0)

    def test_recover_marks_interrupted_and_uncertain(self):
        app = self._make_app()
        _, task = self._submit(app, images=self.images[:2])
        self._wait(task["id"])
        # 人为制造「请求已发出但结果没落库」：把成功项改回 running。
        with self.db._connect() as db:
            db.execute("UPDATE task_items SET state='running' WHERE task_id=? AND ordinal=0", (task["id"],))
        self.store.update_task(task["id"], status="running")
        jobs = self._jobs(app)
        recovered = jobs.recover_interrupted()
        self.assertEqual(recovered, 1)
        state = self.store.get_task(task["id"])
        self.assertEqual(state["status"], "interrupted")
        counts = self.db.counts(task["id"])
        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual(counts["done"], 1)

    def test_continue_unfinished_skips_succeeded_and_requires_uncertain_confirm(self):
        app = self._make_app()
        _, task = self._submit(app, images=self.images[:2])
        self._wait(task["id"])
        # 制造一条 uncertain。
        with self.db._connect() as db:
            db.execute("UPDATE task_items SET state='uncertain' WHERE task_id=? AND ordinal=1",
                       (task["id"],))
        self.store.update_task(task["id"], status="interrupted")
        jobs = self._jobs(app)
        with self.assertRaises(LabelerTaskError):
            jobs.continue_task(task["id"], mode="unfinished", confirm_uncertain=False)
        new_task = jobs.continue_task(task["id"], mode="unfinished", confirm_uncertain=True)
        self.assertEqual(new_task["source_task_id"], task["id"])
        counts = self.db.counts(new_task["id"])
        self.assertEqual(counts["total"], 1, "成功项不应复制到新任务")
        names = [item["name"] for item in self.db.list_items(new_task["id"])]
        self.assertEqual(names, [self.images[1].name], "只复制「结果不确定」的那一张")

    def test_continue_failed_only(self):
        def behavior(profile, messages, options, status):
            raise RuntimeError("500 server error")

        app = self._make_app(_StubModels(behavior))
        _, task = self._submit(app, config=_base_config(retry_limit=0), images=self.images[:2])
        self._wait(task["id"])
        jobs = self._jobs(app)
        new_task = jobs.continue_task(task["id"], mode="failed_only")
        self.assertEqual(self.db.counts(new_task["id"])["total"], 2)

    def test_usage_accumulator_ignores_non_numeric(self):
        merged = aggregate_usage({"total_tokens": 3}, {"total_tokens": 2, "model": "x"})
        self.assertEqual(merged["total_tokens"], 5)
        self.assertNotIn("model", merged)

    def test_local_profile_waits_for_chat_but_still_finishes(self):
        """本地后端：有对话在跑时先让路，对话结束后照样完成打标（不互相卡死）。"""
        app = _StubApp(self.data_dir, config=_StubConfig(kind="local"))
        app.runs = _StubRuns(active_times=2)
        _, task = self._submit(app, images=self.images[:1])
        state = self._wait(task["id"], timeout=15)
        self.assertEqual(state["status"], "done")
        self.assertGreaterEqual(app.runs.calls, 3, "应当先等待对话结束再处理图片")

    def test_online_profile_does_not_poll_chat_runs(self):
        app = _StubApp(self.data_dir, config=_StubConfig(kind="online"))
        app.runs = _StubRuns(active_times=5)
        _, task = self._submit(app, images=self.images[:1])
        state = self._wait(task["id"], timeout=15)
        self.assertEqual(state["status"], "done")
        self.assertEqual(app.runs.calls, 0, "在线 provider 不需要礼让查询")


class LabelerStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LabelerStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_task_carries_module_and_origin(self):
        task = self.store.create_task({"image_refs": [], "config": {}}, source_task_id="tg-src")
        self.assertEqual(task["module"], "labeler")
        self.assertEqual(task["origin_label"], "打标器")
        self.assertEqual(task["source_task_id"], "tg-src")
        reloaded = self.store.get_task(task["id"])
        self.assertEqual(reloaded["module"], "labeler")

    def test_update_rejects_unknown_fields(self):
        task = self.store.create_task({"image_refs": [], "config": {}})
        updated = self.store.update_task(task["id"], status="running", input={"x": 1})
        self.assertEqual(updated["status"], "running")
        self.assertEqual(updated["input"], {"image_refs": [], "config": {}}, "input 不可被 update 改写")

    def test_events_allocate_monotonic_seq_and_reject_unknown(self):
        task = self.store.create_task({"image_refs": [], "config": {}})
        self.assertEqual(self.store.append_event(task["id"], {"type": "status", "message": "x"}), 1)
        self.assertEqual(self.store.append_event(task["id"], {"type": "done"}), 2)
        self.assertEqual([row["seq"] for row in self.store.read_events(task["id"])], [1, 2])
        with self.assertRaises(Exception):
            self.store.append_event(task["id"], {"type": "nope"})

    def test_unknown_status_converges_to_interrupted(self):
        task = self.store.create_task({"image_refs": [], "config": {}})
        path = self.store._record_path(task["id"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "bogus"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.store.get_task(task["id"])["status"], "interrupted")

    def test_referenced_paths_only_for_protected_statuses(self):
        task = self.store.create_task({"image_refs": [{"path": "C:/d/uploads/a.png"}], "config": {}})
        self.assertTrue(self.store.referenced_upload_paths())
        self.store.update_task(task["id"], status="done")
        self.assertEqual(self.store.referenced_upload_paths(), set())


class LabelerExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _records(self):
        return [{
            "name": "a.png", "relative_path": "d/a.png", "tags": ["=cmd", "cat"],
            "confidences": [0.9, 0.5], "description": "一只猫", "preset": "face_lora",
            "output_mode": "both", "model_name": "m", "created_at": 1700000000000,
        }]

    def test_json_contains_confidences(self):
        payload = json.loads(render(self._records(), "json").decode("utf-8"))
        self.assertEqual(payload[0]["confidences"], [0.9, 0.5])

    def test_csv_has_bom_and_formula_guard(self):
        text = render(self._records(), "csv").decode("utf-8-sig")
        self.assertTrue(render(self._records(), "csv").startswith(b"\xef\xbb\xbf"))
        self.assertIn("'=cmd", text)
        self.assertIn("d/a.png", text)

    def test_txt_is_comma_separated(self):
        text = render(self._records(), "txt").decode("utf-8")
        self.assertEqual(text.strip(), "d/a.png, =cmd, cat")

    def test_write_export_stays_in_managed_dir(self):
        artifact = write_export(self.dir / "exports", self._records(), "json", label="../../etc/passwd")
        path = Path(artifact["path"]).resolve()
        self.assertTrue(str(path).startswith(str((self.dir / "exports").resolve())))
        self.assertTrue(path.is_file())

    def test_unknown_format_rejected(self):
        with self.assertRaises(LabelerExportError):
            render(self._records(), "xml")


class LabelerSentenceTests(unittest.TestCase):
    def test_split_chinese_and_english(self):
        sentences = split_sentences("天空是蓝色的。地面有草！Hello world. 再来一句")
        self.assertIn("天空是蓝色的。", sentences)
        self.assertIn("地面有草！", sentences)
        self.assertTrue(any("Hello world." in item for item in sentences))

    def test_stats_marks_degraded_and_counts(self):
        stats = sentence_stats([{"description": "一只猫。一只猫。天空很蓝。"}])
        self.assertTrue(stats["degraded"])
        items = {row["sentence"]: row["count"] for row in stats["items"]}
        self.assertEqual(items["一只猫。"], 2)

    def test_query_filter(self):
        stats = sentence_stats([{"description": "一只猫。天空很蓝。"}], query="猫")
        self.assertEqual(len(stats["items"]), 1)


if __name__ == "__main__":
    unittest.main()
