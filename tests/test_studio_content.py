"""Offline behavior tests for frozen content Skills and Studio followups."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from naiba.core.paths import normalized_path_key
from naiba.skills.agent import SkillAgent
from naiba.skills.catalog import SkillCatalog
from naiba.studio.content import ContentSnapshots, READ_TOOLS, compose_system_prompt
from naiba.studio.service import StudioService
from naiba.studio.store import StudioConflictError
from tests.test_studio import _write_min_catalog, _write_png


def _short_path(path: str) -> str | None:
    """Return the 8.3 spelling of ``path`` when this volume has one."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 32768):
            return None
        return buffer.value or None
    except (AttributeError, OSError):
        return None


class OfflineConfig:
    def __init__(self):
        self.data = {"default_model_key": "online:fixture"}
        self.context_window = 120000
        self.endpoint = "https://fixture.invalid/v1"
        self.steps = 40

    def profile(self, key):
        return {"kind": "online", "name": "offline", "provider_id": "fixture",
                "model": "fixture", "context_window": self.context_window,
                "base_url": self.endpoint, "supports_images_explicit": True}

    def runtime_guard_options(self):
        return {"max_steps": self.steps}


class OfflineModels:
    def __init__(self):
        self.calls = []
        self.last_usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
        self.last_reasoning = ""
        self.last_finish_reason = "stop"
        self.behavior = None

    def complete(self, profile, messages, options, event):
        self.calls.append((copy.deepcopy(messages), dict(options)))
        if self.behavior:
            return self.behavior(messages, options, event)
        text = f"回答{len(self.calls)}。"
        event({"type": "delta", "content": text})
        return text


class StudioContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.skill_dir = root / "skills"
        self.skill_dir.mkdir()
        for name in ("one", "two"):
            folder = self.skill_dir / name
            (folder / "references").mkdir(parents=True)
            (folder / "scripts").mkdir()
            (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\n{name}完整规则。\n请读 references/same.md。", encoding="utf-8")
            (folder / "references" / "same.md").write_text(f"{name}私有参考文档。", encoding="utf-8")
            (folder / "scripts" / "secret.txt").write_text("SCRIPT_SECRET", encoding="utf-8")
            (folder / "evil.py").write_text("print('SCRIPT_SECRET')", encoding="utf-8")
            (folder / "asset.bin").write_bytes(b"BINARY_SECRET\x00")
        paths = SimpleNamespace(data_dir=root / "data", public_dir=root / "public")
        _write_min_catalog(paths.public_dir / "studio")
        self.config = OfflineConfig()
        self.models = OfflineModels()
        self.app = SimpleNamespace(paths=paths, config=self.config, models=self.models,
                                   catalog=SkillCatalog([self.skill_dir]))
        self.service = StudioService(self.app, lambda **kw: {}, lambda **kw: {})
        # Deterministic worker entry: tests explicitly drive each queued task.
        self.service._runner.submit = lambda task_id: None
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.shutdown)
        rows = self.app.catalog.scan()
        self.ids = {row["name"]: row["id"] for row in rows}

    def body(self, **extra):
        return {"model_key": "online:fixture", "model_name": "fixture", "user_request": "第一次输入", **extra}

    def start(self, **extra):
        payload, status = self.service.api_generate(self.body(**extra))
        self.assertEqual(status, 201, payload)
        return payload["task"]

    def finish(self, task):
        self.service._runner._run_task(task["id"])
        result = self.service.jobs.task(task["id"])
        self.assertEqual(result["status"], "done", result.get("error"))
        return result

    def follow(self, task, key, text="继续", **extra):
        payload, status = self.service.api_task_followup(task["id"], {"user_request": text, "request_key": key, **extra})
        self.assertEqual(status, 201, payload)
        return payload["task"]

    def test_template_roles_and_internal_system_are_compatible(self):
        task = self.start(item_id="vi-001-demo", vars={"对象": "人物"}, system_prompt="全局规则")
        self.finish(task)
        messages = self.models.calls[0][0]
        self.assertEqual(messages[0], {"role": "system", "content": "全局规则"})
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("人物 尾行", messages[1]["content"])
        self.assertIn("第一次输入", messages[1]["content"])
        frozen = self.service.freeze_content_input({"content": "阶段", "system": "内部约定", "system_prompt": "作品规则"})
        self.assertEqual(frozen["system"], "作品规则\n\n内部约定")
        self.assertEqual(self.service.freeze_content_input(frozen), frozen)
        self.assertEqual(compose_system_prompt("相同", "相同"), "相同")
        self.assertEqual(self.service.freeze_content_input({"content": "旧任务", "system": "旧约定"})["system"], "旧约定")

    def test_public_model_snapshot_is_rejected_and_internal_novel_identity_is_frozen(self):
        for forged in ({}, {"kind": "online"}, {"base_url_hash": ""}, None):
            result, status = self.service.api_generate(self.body(model_snapshot=forged))
            self.assertEqual(status, 400, result)
            self.assertIn("冻结字段", result["error"])
        data = self.service.freeze_content_input({
            "content": "【第 1 场】正文", "system": "作品规则", "prompt_source": "skill",
            "skill_ids": [self.ids["one"]], "model_key": "online:fixture", "model_name": "fixture",
            "module": "novel", "novel": {"book_id": "book"},
        })
        identity = copy.deepcopy(data["model_snapshot"])
        self.config.endpoint = "https://changed.invalid/v1"
        self.assertEqual(self.service.freeze_content_input(data)["model_snapshot"], identity)
        legacy_skill = copy.deepcopy(data)
        legacy_skill.pop("model_snapshot")
        self.assertNotIn("model_snapshot", self.service.freeze_content_input(legacy_skill))
        task = self.service.jobs.submit(data)
        self.service._runner._run_task(task["id"])
        failed = self.service.jobs.task(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("模型配置已改变", failed["error"])
        self.assertFalse(self.models.calls)

    def test_source_selection_is_strict(self):
        invalid = [
            {"prompt_source": "SKILL"}, {"prompt_source": None},
            {"prompt_source": "skill"}, {"skill_ids": [self.ids["one"]]},
            {"prompt_source": "skill", "skill_ids": "one"},
            {"prompt_source": "skill", "skill_ids": [1]},
            {"prompt_source": "skill", "skill_ids": [""]},
            {"prompt_source": "skill", "skill_ids": [self.ids["one"]], "item_id": "vi-001-demo"},
            {"prompt_source": "skill", "skill_ids": [self.ids["one"]], "source_body": "隐藏模板"},
            {"prompt_source": "skill", "skill_ids": [self.ids["one"]], "vars": {"a": "b"}},
            {"item_id": "vi-001-demo", "custom_id": "custom-wrong"},
            {"system_prompt": ["错误"]}, {"skill_snapshot": {}},
            {"system": "外部不能覆盖内部字段"}, {"history_messages": []},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                payload, status = self.service.api_generate(self.body(**fields))
                self.assertEqual(status, 400, payload)

    @unittest.skipUnless(os.name == "nt", "只有 Windows 存在 8.3 短名")
    def test_short_name_root_still_owns_its_main(self):
        """catalog 报 8.3 短名、解析后的根是长名（CI 的 RUNNER~1），不能判成「Skill 主文不在所属根目录内」。"""
        folder = self.root / "contentskillone"
        (folder / "references").mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: 长名\n---\n长名规则。", encoding="utf-8")
        (folder / "references" / "same.md").write_text("长名参考。", encoding="utf-8")
        row = next(item for item in SkillCatalog([self.root]).scan()
                   if Path(item["root"]).name == "contentskillone")
        short_root, short_main = _short_path(row["root"]), _short_path(row["path"])
        if not short_root or not short_main or os.path.normcase(short_root) == os.path.normcase(row["root"]):
            self.skipTest("该卷未启用 8.3 短名")

        self.app.catalog = SimpleNamespace(scan=lambda: [dict(row, root=short_root, path=short_main)])
        snapshots = ContentSnapshots(self.app.paths.data_dir)
        snapshot = snapshots.freeze(self.app.catalog, [row["id"]])
        view = snapshots.view(snapshot)
        frozen = view.scan()[0]
        self.assertIn("长名规则", view.read_skill_content(frozen["path"]))
        self.assertIn("长名参考", view.read_skill_content(str(Path(frozen["root"]) / "references" / "same.md")))

    def test_full_skill_snapshot_shared_ordered_and_fail_closed(self):
        long_text = "完整主文，不截断。" * 9000
        (self.skill_dir / "one" / "SKILL.md").write_text("---\nname: one\n---\n" + long_text, encoding="utf-8")
        task = self.start(prompt_source="skill", skill_ids=[self.ids["two"], self.ids["one"], self.ids["two"]], system_prompt="手写系统词")
        data = task["input"]
        self.assertEqual(data["skill_ids"], [self.ids["two"], self.ids["one"]])
        self.assertIn(long_text, data["system"])
        self.assertLess(data["system"].index("two完整规则"), data["system"].index(long_text))
        self.assertEqual(data["system"].count("手写系统词"), 1)
        self.assertEqual(self.service.freeze_content_input(data), data)
        snapshots = ContentSnapshots(self.app.paths.data_dir)
        self.assertEqual(len(list(snapshots.directory.glob("*.json"))), 2)
        duplicate = self.service.prepare_generation(self.body(prompt_source="skill", skill_ids=data["skill_ids"], system_prompt="手写系统词"))
        self.assertEqual(duplicate["skill_snapshot"], data["skill_snapshot"])
        self.assertEqual(len(list(snapshots.directory.glob("*.json"))), 2)
        stored = json.loads(self.service._store._record_path(task["id"]).read_text(encoding="utf-8"))
        self.assertNotIn("system", stored["input"])
        self.assertNotIn(long_text, json.dumps(stored, ensure_ascii=False))
        self.assertEqual(len(list((snapshots.directory / "systems").glob("*.json"))), 1)
        self.assertEqual(self.service.jobs.task(task["id"])["input"]["system"], data["system"])
        view = snapshots.view(data["skill_snapshot"])
        roots = {r["name"]: r["root"] for r in view.scan()}
        for name in ("one", "two"):
            ok, text = view.execute("read_file", {"path": str(Path(roots[name]) / "references/same.md")})
            self.assertTrue(ok)
            self.assertIn(name + "私有参考", text)
            for filename in ("scripts/secret.txt", "evil.py", "asset.bin", "../outside.txt"):
                self.assertFalse(view.execute("read_file", {"path": str(Path(roots[name]) / filename)})[0])
        external = self.root / "outside.txt"
        external.write_text("EXTERNAL_SECRET", encoding="utf-8")
        self.assertFalse(view.execute("read_file", {"path": str(external)})[0])
        self.assertFalse(view.execute("read_file", {"path": "references/same.md"})[0])
        self.assertFalse(view.execute("pwsh", {"path": roots["one"]})[0])
        self.assertEqual(view.registry().names(), list(READ_TOOLS))
        listed = view.execute("list_directory", {"path": roots["one"]})[1]
        self.assertNotIn("secret.txt", listed)
        self.assertNotIn("evil.py", listed)
        self.assertNotIn("asset.bin", listed)
        self.assertIn("same.md", view.execute("search_files", {"path": roots["two"], "query": "two私有"})[1])
        shutil.rmtree(self.skill_dir)
        self.finish(task)
        target = snapshots.directory / (data["skill_snapshot"]["skills"][0]["digest"] + ".json")
        target.write_text("{}", encoding="utf-8")
        payload, status = self.service.api_task_followup(task["id"], {"user_request": "继续", "request_key": "corrupt"})
        self.assertEqual(status, 400)
        self.assertIn("快照", payload["error"])

    def test_disguised_binary_is_rejected(self):
        (self.skill_dir / "one" / "references" / "bad.md").write_bytes(b"bad\x00content")
        payload, status = self.service.api_generate(self.body(prompt_source="skill", skill_ids=[self.ids["one"]]))
        self.assertEqual(status, 400)
        self.assertIn("二进制", payload["error"])

    def test_symlink_documents_never_enter_snapshot(self):
        outside = self.root / "secret.md"
        outside.write_text("OUTSIDE", encoding="utf-8")
        link = self.skill_dir / "one" / "references" / "linked.md"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("此 Windows 环境没有创建 symlink 的权限")
        data = self.service.prepare_generation(self.body(prompt_source="skill", skill_ids=[self.ids["one"]]))
        view = ContentSnapshots(self.app.paths.data_dir).view(data["skill_snapshot"])
        self.assertFalse(view.execute("read_file", {"path": str(link)})[0])

    def test_skill_agent_tools_then_three_turns_replay_once(self):
        def behavior(messages, options, event):
            self.assertEqual({spec["name"] for spec in options["tools"]}, set(READ_TOOLS))
            self.assertEqual(options["max_steps"], 40)
            roots = re.findall(r'<skill name="[^"]+" root="([^"]+)">', messages[0]["content"])
            results = "\n".join(str(m.get("content")) for m in messages[1:])
            for name, root in zip(("two", "one"), roots):
                if name + "私有参考" not in results:
                    return json.dumps({"type": "tool", "tool": "read_file", "arguments": {"path": str(Path(root) / "references/same.md")}})
            answer = f"有效回答{sum(m['role'] == 'user' and m.get('content') == '继续' for m in messages) + 1}。"
            event({"type": "delta", "content": answer})
            return answer
        self.models.behavior = behavior
        first = self.finish(self.start(prompt_source="skill", skill_ids=[self.ids["two"], self.ids["one"]], system_prompt="固定系统规则"))
        self.assertEqual(len(self.models.calls), 3)
        self.assertEqual(self.models.calls[-1][0][0]["content"], first["input"]["system"])
        self.assertEqual(first["input"]["system"].count("one完整规则"), 1)
        self.assertTrue(any(m["role"] == "tool" for m in first["trace_messages"]))
        self.assertEqual(first["usage"]["requests"], 3)
        self.assertEqual(sum(row["input_tokens"] for row in first["usage"]["requests_detail"]), 30)
        shutil.rmtree(self.skill_dir)
        second = self.finish(self.follow(first, "turn2"))
        self.assertEqual(len(self.models.calls), 4)
        self.assertEqual(second["thread_id"], first["id"])
        self.assertEqual(second["parent_task_id"], first["id"])
        self.assertFalse(second["source_task_id"])
        self.service.jobs.delete([first["id"]])
        third = self.finish(self.follow(second, "turn3"))
        self.assertEqual(len(self.models.calls), 5)
        messages = self.models.calls[-1][0]
        self.assertEqual(sum(m.get("content") == first["result"] for m in messages), 1)
        self.assertEqual(sum(m.get("content") == second["result"] for m in messages), 1)
        self.assertEqual(sum("one私有参考" in str(m.get("content")) for m in messages), 1)
        self.assertEqual(messages[0]["content"], first["input"]["system"])
        public = self.service.api_task(third["id"])[0]["task"]
        self.assertNotIn("trace_messages", public)
        self.assertTrue(public["input"]["history_messages"])
        self.assertNotIn("one私有参考", json.dumps(public, ensure_ascii=False))
        events = self.service.jobs.events(third["id"])
        self.assertNotIn("one私有参考", json.dumps(events, ensure_ascii=False))
        listed = self.service.api_tasks()[0]["tasks"]
        self.assertEqual(listed[0]["prompt_source"], "skill")
        self.assertEqual(listed[0]["thread_id"], first["id"])

    def test_novel_uses_same_adapter_without_module_switch(self):
        data = self.service.freeze_content_input({"content": "【第 1 场】正文", "system": "作品规则\n\n阶段格式约定",
            "prompt_source": "skill", "skill_ids": [self.ids["one"], self.ids["two"]],
            "module": "novel", "model_key": "online:fixture", "model_name": "fixture", "novel": {"book_id": "book"}})
        task = self.finish(self.service.jobs.submit(data))
        self.assertEqual(task["input"]["module"], "novel")
        self.assertEqual(self.models.calls[0][0][0]["content"], data["system"])
        self.assertEqual(self.models.calls[0][0][-1]["content"], "【第 1 场】正文")
        self.assertEqual(self.service.api_task_followup(task["id"], {"user_request": "继续", "request_key": "novel"})[1], 400)

    def test_image_reference_cache_cannot_overwrite_new_task_invalidation(self):
        from unittest import mock
        from naiba.studio import images as image_refs
        image = _write_png(self.app.paths.data_dir / "uploads" / "late.png")
        store = self.service._store
        entered = threading.Event()
        writer_at_lock = threading.Event()
        release = threading.Event()
        original_lock = store._lock
        original_collect = image_refs.referenced_upload_paths
        failures = []
        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == "reference-writer":
                    writer_at_lock.set()
                original_lock.acquire()
                return self
            def __exit__(self, *args):
                original_lock.release()
        def collect(records):
            entered.set()
            self.assertTrue(original_lock._is_owned(), "引用扫描结果必须在同锁内发布")
            if not release.wait(3):
                raise AssertionError("引用扫描未获准继续")
            return original_collect(records)
        def scan():
            try:
                store.referenced_upload_paths(ttl=0)
            except Exception as exc:
                failures.append(exc)
        def write():
            try:
                store.create_task({"content": "新任务", "model_name": "fixture", "images": [{"path": str(image)}]})
            except Exception as exc:
                failures.append(exc)
        store._lock = ObservedLock()
        reader = threading.Thread(target=scan, name="reference-reader")
        writer = threading.Thread(target=write, name="reference-writer")
        try:
            with mock.patch.object(image_refs, "referenced_upload_paths", side_effect=collect):
                reader.start()
                self.assertTrue(entered.wait(3))
                writer.start()
                self.assertTrue(writer_at_lock.wait(3))
                release.set()
                reader.join(3)
                writer.join(3)
        finally:
            release.set()
            if reader.ident is not None:
                reader.join(3)
            if writer.ident is not None:
                writer.join(3)
            store._lock = original_lock
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(failures, [])
        self.assertIn(normalized_path_key(image), store.referenced_upload_paths())

    def test_followup_images_survive_early_delete_without_base64_on_disk(self):
        image = _write_png(self.app.paths.data_dir / "uploads" / "first.png")
        thumb = _write_png(self.app.paths.data_dir / "uploads" / "first_thumb.webp")
        first = self.finish(self.start(images=[{"path": str(image), "thumb_path": str(thumb), "name": "原图"}], system_prompt="图规则"))
        second = self.finish(self.follow(first, "image2"))
        first_message = copy.deepcopy(self.models.calls[0][0][1])
        self.assertEqual(first_message, self.models.calls[1][0][1])
        self.service.jobs.delete([first["id"]])
        refs = self.service.referenced_upload_paths()
        self.assertIn(normalized_path_key(image), refs)
        self.assertIn(normalized_path_key(thumb), refs)
        third = self.finish(self.follow(second, "image3"))
        self.assertEqual(first_message, self.models.calls[-1][0][1])
        serialized = self.service._store._record_path(third["id"]).read_text(encoding="utf-8")
        self.assertNotIn("base64", serialized)
        image.unlink()
        fourth = self.follow(third, "image4")
        self.service._runner._run_task(fourth["id"])
        self.assertEqual(self.service.jobs.task(fourth["id"])["status"], "failed")

    def test_followup_receipt_and_thread_running_mutex(self):
        first = self.finish(self.start())
        barrier = threading.Barrier(2)
        def follow(key):
            barrier.wait(3)
            return self.service.api_task_followup(first["id"], {"user_request": "继续", "request_key": key})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(follow, ["parallel-one", "parallel-two"]))
        self.assertEqual(sorted(status for _, status in results), [201, 409])
        queued = next(payload["task"] for payload, status in results if status == 201)
        key = self.service.jobs.task(queued["id"])["request_key"]
        again = self.follow(first, key)
        self.assertEqual(again["id"], queued["id"])
        self.assertEqual(self.service.api_task_followup(first["id"], {"user_request": "改变", "request_key": key})[1], 409)
        self.assertEqual(self.service.api_task_followup(first["id"], {"user_request": "继续", "request_key": "x", "system_prompt": "不能改"})[1], 400)
        self.assertEqual(self.service.api_task_followup(first["id"], {"user_request": "继续"})[1], 400)

    def test_retry_keeps_snapshot_history_and_model_even_after_skill_delete(self):
        first = self.finish(self.start(prompt_source="skill", skill_ids=[self.ids["one"]], system_prompt="旧规则"))
        second = self.follow(first, "failed2")
        def fail(messages, options, event):
            event({"type": "delta", "content": "未完成半截"})
            raise RuntimeError("离线故障")
        self.models.behavior = fail
        self.service._runner._run_task(second["id"])
        failed = self.service.jobs.task(second["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["result_partial"], "未完成半截")
        self.assertEqual(self.service.api_task_followup(second["id"], {"user_request": "继续", "request_key": "failed"})[1], 400)
        shutil.rmtree(self.skill_dir)
        self.service.jobs.delete([first["id"]])
        self.models.behavior = None
        retry = self.service.jobs.retry(second["id"], request_key="retry")
        self.assertEqual(retry["input"], failed["input"])
        self.assertEqual(retry["source_task_id"], second["id"])
        self.assertEqual(retry["parent_task_id"], first["id"])
        self.assertEqual(self.service.jobs.retry(second["id"], request_key="retry")["id"], retry["id"])
        with self.assertRaises(StudioConflictError):
            self.service.jobs.retry(second["id"], request_key="different-retry")
        self.finish(retry)
        self.assertNotIn("未完成半截", json.dumps(self.models.calls[-1][0], ensure_ascii=False))
        # Endpoint identity cannot silently drift when the configured provider is edited.
        next_task = self.follow(retry, "endpoint")
        self.config.endpoint = "https://changed.invalid/v1"
        self.service._runner._run_task(next_task["id"])
        self.assertIn("模型配置已改变", self.service.jobs.task(next_task["id"])["error"])

    def test_legacy_retry_does_not_fill_defaults(self):
        legacy = self.service._store.create_task({"content": "旧输入", "system": "旧内部system", "model_key": "online:fixture", "model_name": "fixture"})
        self.service._store.update_task(legacy["id"], status="failed")
        self.config.data["studio"] = {"system_prompt": "新默认", "skill_ids": [self.ids["one"]]}
        retry = self.service.jobs.retry(legacy["id"])
        self.assertEqual(retry["input"], legacy["input"])
        self.finish(retry)
        self.assertEqual(self.models.calls[-1][0][0]["content"], "旧内部system")

    def test_oversized_rules_and_tool_loop_fail_instead_of_done(self):
        self.config.context_window = 1400
        payload, status = self.service.api_generate(self.body(prompt_source="skill", skill_ids=[self.ids["one"]], system_prompt="超长系统词" * 1000))
        self.assertEqual(status, 400)
        self.assertIn("未截断", payload["error"])
        self.assertFalse(self.models.calls)
        self.config.context_window = 120000
        self.config.steps = 1
        def endless(messages, options, event):
            root = re.search(r'root="([^"]+)"', messages[0]["content"]).group(1)
            return json.dumps({"type": "tool", "tool": "list_directory", "arguments": {"path": root}})
        self.models.behavior = endless
        task = self.start(prompt_source="skill", skill_ids=[self.ids["one"]])
        self.service._runner._run_task(task["id"])
        failed = self.service.jobs.task(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("步数上限", failed["error"])
        self.assertTrue(failed["trace_messages"])

    def test_new_generation_uses_updated_skill_but_existing_thread_keeps_original(self):
        first = self.finish(self.start(prompt_source="skill", skill_ids=[self.ids["one"]]))
        (self.skill_dir / "one" / "SKILL.md").write_text("---\nname: one\n---\n全新规则", encoding="utf-8")
        new = self.start(prompt_source="skill", skill_ids=[self.ids["one"]])
        self.assertNotEqual(first["input"]["skill_snapshot"], new["input"]["skill_snapshot"])
        followup = self.follow(first, "original")
        self.assertEqual(followup["input"]["system"], first["input"]["system"])
        self.assertIn("全新规则", new["input"]["system"])

    def test_missing_system_snapshot_rejects_followup_and_retry(self):
        first = self.finish(self.start(prompt_source="skill", skill_ids=[self.ids["one"]]))
        snapshot = first["input"]["skill_snapshot"]
        path = ContentSnapshots(self.app.paths.data_dir).directory / "systems" / (snapshot["system_hash"] + ".json")
        path.unlink()
        response, status = self.service.api_task_followup(first["id"], {"user_request": "继续", "request_key": "broken-system"})
        self.assertEqual(status, 400)
        self.service._store.update_task(first["id"], status="failed")
        with self.assertRaises(ValueError):
            self.service.jobs.retry(first["id"])
        self.assertTrue(self.service._store.delete_task(first["id"]))

    def test_content_tool_read_limits_do_not_silently_cut_a_long_line(self):
        path = self.skill_dir / "one" / "references" / "same.md"
        path.write_text("一行" * 15000, encoding="utf-8")
        task = self.start(prompt_source="skill", skill_ids=[self.ids["one"]])
        view = ContentSnapshots(self.app.paths.data_dir).view(task["input"]["skill_snapshot"])
        ok, message = view.execute("read_file", {"path": str(path)})
        self.assertFalse(ok)
        self.assertIn("未截断", message)

    def test_failure_after_tool_read_preserves_trace_and_cancel_does_not_complete(self):
        state = {"step": 0}
        def fail_after_read(messages, options, event):
            state["step"] += 1
            if state["step"] == 1:
                root = re.search(r'root="([^"]+)"', messages[0]["content"]).group(1)
                return json.dumps({"type": "tool", "tool": "read_file", "arguments": {"path": str(Path(root) / "references/same.md")}})
            event({"type": "delta", "content": "保留半截"})
            raise RuntimeError("第二次调用失败")
        self.models.behavior = fail_after_read
        task = self.start(prompt_source="skill", skill_ids=[self.ids["one"]])
        self.service._runner._run_task(task["id"])
        failed = self.service.jobs.task(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(any(m["role"] == "tool" for m in failed["trace_messages"]))
        self.assertEqual(failed["result_partial"], "保留半截")
        self.assertEqual(failed["usage"]["requests"], 1)
        def cancel(messages, options, event):
            event({"type": "delta", "content": "取消前半截"})
            options["cancel_event"].set()
            return "不应成功"
        self.models.behavior = cancel
        task = self.start(prompt_source="skill", skill_ids=[self.ids["one"]])
        self.service._runner._run_task(task["id"])
        cancelled = self.service.jobs.task(task["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["result_partial"], "取消前半截")
        self.assertEqual(self.service.api_task_followup(task["id"], {"user_request": "继续", "request_key": "cancel"})[1], 400)
        queued = self.start()
        self.assertTrue(self.service.jobs.cancel(queued["id"]))
        before = len(self.models.calls)
        self.service._runner._run_task(queued["id"])
        self.assertEqual(len(self.models.calls), before)
        self.assertFalse(self.service._store.claim_task(queued["id"]))

    def test_cancel_at_final_flush_wins_and_finished_task_rejects_cancel(self):
        from unittest import mock
        from naiba.studio.runner import _DeltaBuffer
        task = self.start()
        runner = self.service._runner
        flushing = threading.Event()
        proceed = threading.Event()
        real_flush = _DeltaBuffer.flush
        failures = []
        def flush(buffer):
            real_flush(buffer)
            if getattr(buffer, "_audit_paused", False):
                return
            # Initial status flush happens before complete. Pause only after
            # the captured model call has returned, at the final flush.
            if self.models.calls:
                buffer._audit_paused = True
                flushing.set()
                if not proceed.wait(3):
                    raise AssertionError("终态flush未获准继续")
        def run():
            try:
                runner._run_task(task["id"])
            except Exception as exc:
                failures.append(exc)
        worker = threading.Thread(target=run)
        try:
            with mock.patch.object(_DeltaBuffer, "flush", flush):
                worker.start()
                self.assertTrue(flushing.wait(3))
                self.assertTrue(runner.cancel(task["id"]))
                proceed.set()
                worker.join(3)
        finally:
            proceed.set()
            if worker.ident is not None:
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        cancelled = self.service.jobs.task(task["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["result_partial"])
        self.assertNotIn("done", [row["type"] for row in self.service.jobs.events(task["id"])])
        completed = self.finish(self.start())
        # Emulate the cleanup interval after done was written but the event
        # registry entry is not yet removed.
        event = threading.Event()
        runner._cancel_events[completed["id"]] = event
        try:
            self.assertFalse(runner.cancel(completed["id"]))
            self.assertFalse(event.is_set())
        finally:
            runner._cancel_events.pop(completed["id"], None)

    def test_runtime_document_growth_stops_before_next_model_call(self):
        self.config.context_window = 4000
        (self.skill_dir / "one" / "references" / "same.md").write_text("参考资料" * 2000, encoding="utf-8")
        def read_large(messages, options, event):
            root = re.search(r'root="([^"]+)"', messages[0]["content"]).group(1)
            return json.dumps({"type": "tool", "tool": "read_file", "arguments": {"path": str(Path(root) / "references/same.md")}})
        self.models.behavior = read_large
        task = self.start(prompt_source="skill", skill_ids=[self.ids["one"]])
        self.service._runner._run_task(task["id"])
        failed = self.service.jobs.task(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(self.models.calls), 1)
        self.assertIn("上下文", failed["error"])

    def test_agent_override_is_explicit_and_default_behavior_unchanged(self):
        class EmptyCatalog:
            def scan(self):
                return []
        agent = SkillAgent(EmptyCatalog(), SimpleNamespace(workspace=""), self.models.complete)
        profile = self.config.profile("fixture")
        args = ("", [{"role": "user", "content": "输入"}], profile,
                {"system_prompt_override": "不能从options覆盖"}, {"mode": "exclusive", "skill_ids": []}, [], "原系统", [], lambda e: None)
        agent.run(*args, run_context={})
        self.assertIn("原系统", self.models.calls[-1][0][0]["content"])
        self.assertNotIn("不能从options覆盖", self.models.calls[-1][0][0]["content"])
        agent.run(*args, run_context={}, system_prompt_override="服务端冻结system")
        self.assertEqual(self.models.calls[-1][0][0]["content"], "服务端冻结system")


if __name__ == "__main__":
    unittest.main()
