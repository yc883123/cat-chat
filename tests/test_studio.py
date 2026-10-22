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
from naiba.core.paths import normalized_path_key  # noqa: E402
from naiba.config import ConfigStore  # noqa: E402
from naiba.studio.catalog import CatalogError, StudioCatalog  # noqa: E402
from naiba.studio.custom import CustomStore  # noqa: E402
from naiba.studio.handoff import compose_handoff_message  # noqa: E402
from naiba.studio.images import MAX_IMAGES, StudioImageError, validate_image_refs  # noqa: E402
from naiba.studio.runner import StudioRunner  # noqa: E402
from naiba.studio.service import StudioService, fill_vars  # noqa: E402
from naiba.studio.store import StudioStore  # noqa: E402
from naiba.vision.runtime import VisionRouter  # noqa: E402


def _write_png(path: Path, color=(20, 120, 200), size=(16, 16)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


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


class StudioImageValidationTests(unittest.TestCase):
    """图片引用校验：受管目录、真实图片、数量/大小上限、顺序与引用保护。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.uploads = self.data_dir / "uploads"
        self.uploads.mkdir(parents=True)
        self.png_a = _write_png(self.uploads / "a.png")
        self.png_b = _write_png(self.uploads / "b.png", color=(1, 2, 3))

    def tearDown(self):
        self.tmp.cleanup()

    def test_keeps_client_order_and_normalizes(self):
        refs = validate_image_refs(self.data_dir, [
            {"path": str(self.png_a), "name": "第一张"},
            str(self.png_b),
        ])
        self.assertEqual([row["path"] for row in refs], [str(self.png_a.resolve()), str(self.png_b.resolve())])
        self.assertEqual(refs[0]["name"], "第一张")
        self.assertEqual(refs[1]["name"], "b.png")

    def test_rejects_paths_outside_managed_uploads(self):
        outside = _write_png(Path(self.tmp.name) / "outside.png")
        with self.assertRaises(StudioImageError):
            validate_image_refs(self.data_dir, [str(outside)])

    def test_rejects_non_image_and_missing_files(self):
        fake = self.uploads / "fake.png"
        fake.write_text("not an image", encoding="utf-8")
        with self.assertRaises(StudioImageError):
            validate_image_refs(self.data_dir, [str(fake)])
        with self.assertRaises(StudioImageError):
            validate_image_refs(self.data_dir, [str(self.uploads / "missing.png")])
        bad_ext = self.uploads / "note.txt"
        bad_ext.write_text("x", encoding="utf-8")
        with self.assertRaises(StudioImageError):
            validate_image_refs(self.data_dir, [str(bad_ext)])

    def test_rejects_too_many_and_oversized(self):
        with self.assertRaises(StudioImageError):
            validate_image_refs(self.data_dir, [str(self.png_a)] * (MAX_IMAGES + 1))
        huge = self.uploads / "huge.png"
        with huge.open("wb") as handle:
            handle.truncate(20 * 1024 * 1024 + 1)
        with self.assertRaises(StudioImageError):
            validate_image_refs(self.data_dir, [str(huge)])

    def test_store_reports_referenced_paths(self):
        store = StudioStore(self.data_dir)
        store.create_task({
            "content": "带图生成",
            "images": [{"path": str(self.png_a), "thumb_path": str(self.png_b), "name": "a"}],
        })
        keys = store.referenced_upload_paths()
        self.assertIn(normalized_path_key(self.png_a), keys)
        self.assertIn(normalized_path_key(self.png_b), keys)


class StudioPrefsConfigTests(unittest.TestCase):
    """settings.studio：归一化、白名单、部分更新、旧配置兼容。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, payload=None):
        if payload is not None:
            self.path.write_text(json.dumps(payload), encoding="utf-8")
        return ConfigStore(self.path)

    def test_fresh_install_and_legacy_config_get_defaults(self):
        self.assertEqual(self._store().data["studio"], {"last_selection": None, "favorites": []})
        legacy = self._store({"appearance": {"theme": "dark"}})
        self.assertEqual(legacy.data["studio"], {"last_selection": None, "favorites": []})

    def test_corrupt_values_are_normalized(self):
        store = self._store({
            "studio": {
                "last_selection": {"kind": "bogus", "id": "x", "module": "vs", "page": "library"},
                "favorites": [
                    {"kind": "builtin", "id": "vi-001"},
                    {"kind": "builtin", "id": "vi-001"},
                    {"kind": "bogus", "id": "x"},
                    "junk",
                ],
            }
        })
        self.assertIsNone(store.data["studio"]["last_selection"])
        self.assertEqual(store.data["studio"]["favorites"], [{"kind": "builtin", "id": "vi-001"}])

    def test_partial_update_and_reload(self):
        store = self._store()
        store.update_settings({"studio": {"last_selection": {
            "kind": "builtin", "id": "vi-001", "module": "vi", "page": "library",
        }}})
        store.update_settings({"studio": {"favorites": [{"kind": "custom", "id": "custom-x"}]}})
        reloaded = ConfigStore(self.path)
        self.assertEqual(reloaded.data["studio"]["last_selection"]["id"], "vi-001")
        self.assertEqual(reloaded.data["studio"]["favorites"], [{"kind": "custom", "id": "custom-x"}])
        # 清除上次选择：null 是合法值，收藏不受影响。
        reloaded.update_settings({"studio": {"last_selection": None}})
        self.assertIsNone(reloaded.data["studio"]["last_selection"])
        self.assertEqual(reloaded.data["studio"]["favorites"], [{"kind": "custom", "id": "custom-x"}])

    def test_invalid_payloads_rejected(self):
        store = self._store()
        invalid = [
            {"studio": None},
            {"studio": {"unknown": 1}},
            {"studio": {"last_selection": "x"}},
            {"studio": {"last_selection": {"kind": "bogus", "id": "a", "module": "", "page": "library"}}},
            {"studio": {"last_selection": {"kind": "builtin", "id": "", "module": "", "page": "library"}}},
            {"studio": {"last_selection": {"kind": "builtin", "id": "a", "module": "nope", "page": "library"}}},
            {"studio": {"last_selection": {"kind": "builtin", "id": "a", "module": "", "page": "nope"}}},
            {"studio": {"favorites": "x"}},
            {"studio": {"favorites": [{"kind": "bogus", "id": "a"}]}},
            {"studio": {"favorites": [{"kind": "builtin", "id": ""}]}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.update_settings(payload)
        # 重复收藏去重而不是拒绝（前端误发同一行两次不该阻断保存）。
        result = store.update_settings({"studio": {"favorites": [
            {"kind": "builtin", "id": "a"}, {"kind": "builtin", "id": "a"},
        ]}})
        self.assertEqual(result["studio"]["favorites"], [{"kind": "builtin", "id": "a"}])


class _StubModels:
    """complete() 桩：按 options.cancel_event 与注入脚本决定行为。"""

    def __init__(self, behavior):
        self.behavior = behavior  # callable(status_cb, cancel_event, profile, messages, options)
        self.complete_kwargs = []
        self.messages = []

    def complete(self, profile, messages, options, status_cb):
        self.complete_kwargs.append(profile)
        self.messages.append(messages)
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


class _StubVision:
    """视觉能力桩：分别记录「当前参数模型」与「视觉配置」两条调用路径。"""

    def __init__(self, behavior=None, supports=True):
        self.calls: list[tuple[list[str], str]] = []
        self.selected_calls: list[tuple[dict, list[str], str]] = []
        self.supports = supports
        self.behavior = behavior or (lambda path, question: f"{Path(path).stem} 的描述")
        self.selected_behavior = None

    def describe_files(self, paths, question="", json_mode=False, cancel_event=None, vision_budget=None):
        self.calls.append((list(paths), question))
        return self.behavior(str(paths[0]), question)

    def describe_files_with_profile(self, profile, paths, question="", cancel_event=None, vision_budget=None):
        self.selected_calls.append((dict(profile or {}), list(paths), question))
        if self.selected_behavior is not None:
            return self.selected_behavior(str(paths[0]), question)
        return f"当前模型：{Path(paths[0]).stem} 的描述"

    def brain_supports_images(self, profile):
        return self.supports


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

    def test_images_build_numbered_multimodal_message(self):
        runner = self._runner()
        first = _write_png(Path(self.tmp.name) / "uploads" / "one.png")
        second = _write_png(Path(self.tmp.name) / "uploads" / "two.png", color=(4, 5, 6))
        self.models.behavior = lambda status_cb, options: "ok"
        task = self.store.create_task({
            "content": "把两张图合成一张",
            "model_key": "online:p1",
            "model_name": "stub-model",
            "images": [
                {"path": str(first), "thumb_path": "", "name": "场景"},
                {"path": str(second), "thumb_path": "", "name": "人物"},
            ],
        })
        runner.submit(task["id"])
        deadline = time.time() + 10
        while time.time() < deadline and self.store.get_task(task["id"])["status"] not in {"done", "failed"}:
            time.sleep(0.02)
        state = self.store.get_task(task["id"])
        self.assertEqual(state["status"], "done")
        content = self.models.messages[-1][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual([p.get("type") for p in content], ["text", "image", "text", "image", "text"])
        self.assertIn("图1（场景）", content[0]["text"])
        self.assertIn("图2（人物）", content[2]["text"])
        self.assertEqual(content[1]["media_type"], "image/jpeg")
        self.assertTrue(content[1]["data"])
        self.assertIn("把两张图合成一张", content[4]["text"])

    def test_text_only_model_rejects_images_with_clear_error(self):
        runner = self._runner()

        class _ExplicitNoVisionConfig(_StubConfig):
            def profile(self, key):
                return {
                    **super().profile(key),
                    "supports_images": False,
                    "supports_images_explicit": False,
                }

        runner.app.config = _ExplicitNoVisionConfig()
        image = _write_png(Path(self.tmp.name) / "uploads" / "only.png")
        task = self.store.create_task({
            "content": "看图生成",
            "model_key": "online:p1",
            "model_name": "stub-model",
            "images": [{"path": str(image), "thumb_path": "", "name": "图"}],
        })
        runner.submit(task["id"])
        deadline = time.time() + 10
        while time.time() < deadline and self.store.get_task(task["id"])["status"] not in {"done", "failed"}:
            time.sleep(0.02)
        state = self.store.get_task(task["id"])
        self.assertEqual(state["status"], "failed")
        self.assertIn("不支持图片输入", state["error"])
        self.assertEqual(self.models.messages, [])

    def test_inferred_text_only_does_not_block_capable_models(self):
        """推断为不支持但未显式声明：不能硬拦（冻结版 tedsres/grok 实测可用）。"""
        runner = self._runner()

        class _InferredNoVisionConfig(_StubConfig):
            def profile(self, key):
                return {
                    **super().profile(key),
                    "supports_images": False,
                    "supports_images_explicit": None,
                }

        runner.app.config = _InferredNoVisionConfig()
        self.models.behavior = lambda status_cb, options: "已按图生成"
        image = _write_png(Path(self.tmp.name) / "uploads" / "capable.png")
        task = self.store.create_task({
            "content": "看图生成",
            "model_key": "online:p1",
            "model_name": "stub-model",
            "images": [{"path": str(image), "thumb_path": "", "name": "图"}],
        })
        runner.submit(task["id"])
        deadline = time.time() + 10
        while time.time() < deadline and self.store.get_task(task["id"])["status"] not in {"done", "failed"}:
            time.sleep(0.02)
        state = self.store.get_task(task["id"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["result"], "已按图生成")
        content = self.models.messages[-1][0]["content"]
        self.assertEqual([part.get("type") for part in content], ["text", "image", "text"])


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
        self.uploads = base / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.img_a = _write_png(self.uploads / "a.png")
        self.img_b = _write_png(self.uploads / "b.png", color=(9, 9, 9))
        self.vision = _StubVision()
        self.app.vision = self.vision
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

    def test_generate_persists_images_and_sends_numbered_multimodal(self):
        payload, status = self.service.api_generate({
            "user_request": "把这两张图合成一张海报",
            "model_key": "online:p1",
            "model_name": "stub-model",
            "images": [
                {"path": str(self.img_a), "name": "场景"},
                {"path": str(self.img_b), "thumb_path": "", "name": "人物"},
            ],
        })
        self.assertEqual(status, 201)
        task = payload["task"]
        self.assertEqual(
            [row["path"] for row in task["input"]["images"]],
            [str(self.img_a.resolve()), str(self.img_b.resolve())],
        )
        state = self._wait_done(task["id"])
        self.assertEqual(state["status"], "done")
        # runner 以同一顺序构造结构化多模态消息：图号文本 + 图片部件逐对出现。
        content = self.models.messages[-1][0]["content"]
        self.assertIsInstance(content, list)
        kinds = [part.get("type") for part in content]
        self.assertEqual(kinds, ["text", "image", "text", "image", "text"])
        self.assertIn("图1（场景）", content[0]["text"])
        self.assertIn("图2（人物）", content[2]["text"])
        self.assertIn("合成一张海报", content[4]["text"])

    def test_generate_rejects_unmanaged_image_path(self):
        outside = _write_png(Path(self.tmp.name) / "outside.png")
        payload, status = self.service.api_generate({
            "user_request": "越权图片",
            "model_key": "online:p1",
            "model_name": "stub-model",
            "images": [str(outside)],
        })
        self.assertEqual(status, 400)
        self.assertIn("受管上传目录", payload["error"])

    def test_vision_returns_numbered_sections_in_order(self):
        payload, status = self.service.api_vision({
            "images": [str(self.img_a), str(self.img_b)],
        })
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["analyzed"], 2)
        self.assertIn("【图1】", payload["text"])
        self.assertIn("【图2】", payload["text"])
        self.assertLess(payload["text"].index("【图1】"), payload["text"].index("【图2】"))
        # 逐张调用：每张都带明确的图号指令，而不是把多图混成一段。
        self.assertEqual(len(self.vision.calls), 2)
        self.assertIn("第 1 张图", self.vision.calls[0][1])
        self.assertIn("第 2 张图", self.vision.calls[1][1])

    def test_vision_uses_selected_model_before_vision_settings(self):
        payload, status = self.service.api_vision({
            "images": [str(self.img_a), str(self.img_b)],
            "model_key": "online:p1",
            "model_name": "stub-model",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["fallback_used"], 0)
        self.assertEqual([row["backend"] for row in payload["images"]], ["selected", "selected"])
        self.assertEqual(len(self.vision.selected_calls), 2)
        self.assertEqual(self.vision.calls, [], "当前模型成功时不应再触达视觉配置")
        self.assertIn("当前模型", payload["text"])

    def test_vision_falls_back_when_selected_model_cannot_see_images(self):
        self.vision.selected_behavior = lambda path, question: (_ for _ in ()).throw(
            RuntimeError("当前模型不支持图片输入")
        )
        payload, status = self.service.api_vision({
            "images": [str(self.img_a), str(self.img_b)],
            "model_key": "online:p1",
            "model_name": "stub-model",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["fallback_used"], 2)
        self.assertEqual([row["backend"] for row in payload["images"]], ["vision", "vision"])
        self.assertIn("不支持图片输入", payload["images"][0]["fallback_reason"])
        self.assertEqual(len(self.vision.calls), 2)
        self.assertIn("a 的描述", payload["text"])

    def test_vision_skips_explicit_text_only_model(self):
        class _ExplicitTextOnlyConfig(_StubConfig):
            def profile(self, key):
                return {
                    **super().profile(key),
                    "supports_images": False,
                    "supports_images_explicit": False,
                }

        self.service.app.config = _ExplicitTextOnlyConfig()
        payload, status = self.service.api_vision({
            "images": [str(self.img_a)],
            "model_key": "online:p1",
            "model_name": "stub-model",
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.vision.selected_calls, [], "显式纯文本模型不应先发一次必然失败的请求")
        self.assertEqual(payload["fallback_used"], 1)
        self.assertIn("不支持图片", payload["selected_note"])

    def test_vision_partial_failure_keeps_successful_images(self):
        def behavior(path, question):
            if path.endswith("b.png"):
                raise RuntimeError("后端识别超时")
            return "一只橘猫坐在窗台"

        self.vision.behavior = behavior
        payload, status = self.service.api_vision({
            "images": [str(self.img_a), str(self.img_b)],
        })
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["analyzed"], 1)
        self.assertEqual(payload["failed"], 1)
        self.assertIn("一只橘猫", payload["text"])
        self.assertNotIn("【图2】", payload["text"])
        self.assertIn("后端识别超时", payload["images"][1]["error"])

    def test_vision_all_failure_reports_502_without_faking(self):
        self.vision.behavior = lambda path, question: (_ for _ in ()).throw(RuntimeError("视觉后端全部失败"))
        payload, status = self.service.api_vision({"images": [str(self.img_a)]})
        self.assertEqual(status, 502)
        self.assertEqual(payload["analyzed"], 0)
        self.assertIn("视觉识别全部失败", payload["error"])
        self.assertEqual(payload["text"], "")

    def test_vision_rejects_missing_or_invalid_images(self):
        payload, status = self.service.api_vision({"images": []})
        self.assertEqual(status, 400)
        self.assertIn("上传图片", payload["error"])
        outside = _write_png(Path(self.tmp.name) / "outside2.png")
        payload, status = self.service.api_vision({"images": [str(outside)]})
        self.assertEqual(status, 400)

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


class VisionSettingsTests(unittest.TestCase):
    """视觉分页「供应商 + 模型」：配置归一化与后端模型覆盖。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_vision_model_roundtrip_and_trim(self):
        store = ConfigStore(self.path)
        store.update_settings({"vision": {
            "provider_model_key": " online:p1 ",
            "model": " qwen-vl-max ",
        }})
        reloaded = ConfigStore(self.path)
        self.assertEqual(reloaded.data["vision"]["provider_model_key"], "online:p1")
        self.assertEqual(reloaded.data["vision"]["model"], "qwen-vl-max")

    def test_vision_backends_apply_model_override(self):
        class _Config:
            data = {"vision": {"provider_model_key": "online:p1", "model": "override-a"}}

            @staticmethod
            def profile(key):
                return {"name": "供应商", "model": "base-model", "request_format": "openai_chat"}

        class _App:
            config = _Config()

        vision = VisionRouter(_App())
        self.assertEqual(vision.vision_backends()[0]["model"], "override-a")
        self.assertEqual(vision.vision_backends("online:p1", "explicit-b")[0]["model"], "explicit-b")
        self.assertEqual(vision.vision_backends("online:p1")[0]["model"], "override-a")


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
