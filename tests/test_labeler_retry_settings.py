"""贴签匠回归：单档案额度原子保存，以及 Skill 快照任务继续。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from naiba.labeler.jobs import LabelerTaskError, normalize_config
from naiba.labeler.service import LabelerService
from naiba.skills.catalog import SkillCatalog
from tests import test_labeler as fixtures
from tests import test_labeler_concurrency as concurrent_fixtures


class LabelerSettingsPatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app = concurrent_fixtures._StubApp(Path(temporary.name))
        self.service = LabelerService(app)
        self.addCleanup(self.service.shutdown)
        self.service.store.write_settings({"provider_limits": {"online:existing": 3}})

    def test_patch_preserves_other_providers_and_returns_full_settings(self):
        payload, status = self.service.api_update_settings(
            {"provider_limits_patch": {"online:a": 2}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider_limits"], {"online:existing": 3, "online:a": 2})
        payload, status = self.service.api_update_settings(
            {"provider_limits_patch": {"online:a": 5}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider_limits"], {"online:existing": 3, "online:a": 5})

    def test_concurrent_patches_hold_lock_across_read_modify_write(self):
        original_read = self.service.store.read_settings
        first_read = threading.Event()
        second_read = threading.Event()
        second_started = threading.Event()
        release = threading.Event()
        read_count = 0
        count_lock = threading.Lock()

        def read_settings():
            nonlocal read_count
            snapshot = original_read()
            with count_lock:
                read_count += 1
                first = read_count == 1
            if first:
                first_read.set()
                if not release.wait(3):
                    raise RuntimeError("等待并发保存测试释放超时")
            else:
                second_read.set()
            return snapshot

        def second_patch():
            second_started.set()
            return self.service.api_update_settings({"provider_limits_patch": {"online:b": 7}})

        with mock.patch.object(self.service.store, "read_settings", side_effect=read_settings):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.service.api_update_settings,
                                    {"provider_limits_patch": {"online:a": 2}})
                try:
                    self.assertTrue(first_read.wait(3))
                    second = pool.submit(second_patch)
                    self.assertTrue(second_started.wait(3))
                    self.assertFalse(second_read.wait(0.1), "第二个更新不能读取未提交快照")
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3)[1], 200)
                self.assertEqual(second.result(timeout=3)[1], 200)
        self.assertEqual(original_read()["provider_limits"],
                         {"online:existing": 3, "online:a": 2, "online:b": 7})

    def test_legacy_full_replace_and_clear_remain_supported(self):
        payload, status = self.service.api_update_settings({"provider_limits": {"online:a": "4"}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider_limits"], {"online:a": 4})
        payload, status = self.service.api_update_settings({"provider_limits": {}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider_limits"], {})

    def test_invalid_requests_never_replace_or_partially_merge_settings(self):
        requests = [
            None, [], {}, {"unexpected": {}},
            {"provider_limits": {}, "provider_limits_patch": {"online:a": 2}},
            {"provider_limits_patch": None}, {"provider_limits_patch": []},
            {"provider_limits_patch": {}}, {"provider_limits": None},
            {"provider_limits_patch": {"": 2}},
            {"provider_limits_patch": {"online:a": 2, " online:a ": 3}},
            {"provider_limits_patch": {"online:a": 2, "missing": 4}},
        ]
        for value in (True, False, 0, 10, 2.5, "2.5", float("inf"), float("nan")):
            requests.append({"provider_limits_patch": {"online:a": value}})
        for body in requests:
            with self.subTest(body=body):
                _, status = self.service.api_update_settings(body)
                self.assertEqual(status, 400)
                self.assertEqual(self.service.store.read_settings()["provider_limits"],
                                 {"online:existing": 3})

    def test_write_failure_does_not_report_success_or_mutate_saved_limits(self):
        with mock.patch.object(self.service.store, "_write_json", side_effect=OSError("disk full")):
            payload, status = self.service.api_update_settings(
                {"provider_limits_patch": {"online:a": 2}})
        self.assertEqual(status, 500)
        self.assertIn("保存失败", payload["error"])
        self.assertEqual(self.service.store.read_settings()["provider_limits"],
                         {"online:existing": 3})


class LabelerConcurrencySettingsTests(unittest.TestCase):
    """并发打标配置（开关 + 槽位 + 监督员）随贴签匠设置持久化。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app = concurrent_fixtures._StubApp(Path(temporary.name))
        self.service = LabelerService(app)
        self.addCleanup(self.service.shutdown)
        self.service.store.write_settings({"provider_limits": {"online:existing": 3}})

    def _config(self, **overrides):
        config = {
            "enabled": True,
            "workers": [
                {"model_key": "online:a", "model_name": "gpt-x"},
                {"model_key": "online:b", "model_name": "claude-y"},
            ],
            "supervisor": {"model_key": "online:a", "model_name": "gpt-x"},
        }
        config.update(overrides)
        return config

    def test_save_and_read_back_concurrency_config(self):
        config = self._config()
        payload, status = self.service.api_update_settings({"concurrency": config})
        self.assertEqual(status, 200)
        self.assertEqual(payload["concurrency"], config)
        read, status = self.service.api_settings()
        self.assertEqual(status, 200)
        self.assertEqual(read["concurrency"], config)

    def test_saving_concurrency_keeps_provider_limits_and_vice_versa(self):
        config = self._config()
        self.service.api_update_settings({"concurrency": config})
        payload, status = self.service.api_update_settings(
            {"provider_limits_patch": {"online:a": 2}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider_limits"], {"online:existing": 3, "online:a": 2})
        self.assertEqual(payload["concurrency"], config)
        self.assertEqual(self.service.store.read_settings()["concurrency"], config)

    def test_empty_slots_are_allowed_so_half_configured_layouts_survive(self):
        config = self._config(workers=[{"model_key": "", "model_name": ""}])
        payload, status = self.service.api_update_settings({"concurrency": config})
        self.assertEqual(status, 200)
        self.assertEqual(payload["concurrency"]["workers"], [{"model_key": "", "model_name": ""}])

    def test_invalid_concurrency_is_rejected_without_touching_saved_config(self):
        saved = self._config(enabled=False)
        self.service.api_update_settings({"concurrency": saved})
        requests = [
            [], "x", {"enabled": "yes"}, {"workers": [{"model_key": "a", "model_name": "b"}]},
            {"enabled": True, "workers": []},
            {"enabled": True, "workers": [{"model_key": "a", "model_name": "b"}] * 10},
            {"enabled": True, "workers": ["x"]},
            {"enabled": True, "workers": [{"model_key": 1, "model_name": "b"}]},
            {"enabled": True, "workers": [{"model_key": "a", "model_name": "b"}],
             "supervisor": "x"},
            {"enabled": True, "workers": [{"model_key": "a", "model_name": "b"}],
             "supervisor": {"model_key": "", "model_name": "", "extra": 1}},
            {"enabled": True, "workers": [{"model_key": "a", "model_name": "b"}],
             "unknown": True},
        ]
        for value in requests:
            with self.subTest(value=value):
                _, status = self.service.api_update_settings({"concurrency": value})
                self.assertEqual(status, 400)
                self.assertEqual(self.service.store.read_settings()["concurrency"], saved)

    def test_concurrency_and_provider_limits_cannot_mix_in_one_request(self):
        _, status = self.service.api_update_settings({
            "concurrency": self._config(),
            "provider_limits_patch": {"online:a": 2},
        })
        self.assertEqual(status, 400)
        self.assertEqual(self.service.store.read_settings()["concurrency"], {})
        self.assertEqual(self.service.store.read_settings()["provider_limits"],
                         {"online:existing": 3})


class LabelerSkillRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        self.image = fixtures._write_png(self.data_dir / "uploads" / "cat.png")
        self.skill_dir = self.data_dir / "skills" / "caption"
        self.skill_dir.mkdir(parents=True)
        self.skill_file = self.skill_dir / "SKILL.md"
        self.skill_file.write_text(
            "---\nname: caption\ndescription: 图片打标\n---\n\n只描述真实可见的内容。",
            encoding="utf-8",
        )
        self.app = fixtures._StubApp(self.data_dir)
        self.app.catalog = SkillCatalog([self.skill_dir.parent])
        self.skill_id = self.app.catalog.scan()[0]["id"]
        self.service = LabelerService(self.app)
        self.addCleanup(self.service.shutdown)
        # 这里只验证存储与提交契约；不启动模型工作线程。
        patcher = mock.patch.object(self.service.runner, "submit")
        self.submit = patcher.start()
        self.addCleanup(patcher.stop)

    def failed_task(self, **overrides):
        config = fixtures._base_config(engine="chat", output_mode="auto", language="auto",
                                       skill_id=self.skill_id, system_prompt="保留手写规则")
        config.update(overrides)
        payload, status = self.service.api_submit({
            "image_refs": [{"path": str(self.image), "name": "cat.png"}], "config": config,
        })
        self.assertEqual(status, 201, payload)
        task_id = payload["task"]["id"]
        item = self.service.db.list_items(task_id)[0]
        self.service.db.fail_item(task_id, item["item_id"], error="401", category="auth")
        self.service.store.update_task(task_id, status="failed")
        return self.service.store.get_task(task_id)

    def test_unchanged_skill_can_retry_with_identical_snapshot_and_fingerprint(self):
        source = self.failed_task()
        payload, status = self.service.api_retry(source["id"], {"mode": "failed_only"})
        self.assertEqual(status, 201, payload)
        retried = payload["task"]
        self.assertEqual(retried["source_task_id"], source["id"])
        self.assertEqual(retried["input"]["config"], source["input"]["config"])
        self.assertNotEqual(retried["id"], source["id"])
        self.assertEqual(self.service.store.get_task(source["id"])["input"], source["input"])

    def test_changed_skill_is_rejected_before_creating_retry(self):
        source = self.failed_task()
        self.skill_file.write_text("# 新规则", encoding="utf-8")
        payload, status = self.service.api_retry(source["id"], {"mode": "failed_only"})
        self.assertEqual(status, 400)
        self.assertIn("Skill 已变化", payload["error"])
        self.assertEqual(self.submit.call_count, 1)

    def test_skill_change_during_normalization_is_also_rejected(self):
        source = self.failed_task()
        revision = source["input"]["config"]["skill_revision"]
        with mock.patch("naiba.labeler.jobs._skill_revision", side_effect=[revision, "changed"]):
            payload, status = self.service.api_retry(source["id"], {"mode": "failed_only"})
        self.assertEqual(status, 400)
        self.assertIn("Skill 已变化", payload["error"])
        self.assertEqual(self.submit.call_count, 1)

    def test_client_cannot_supply_internal_skill_revision(self):
        raw = fixtures._base_config(engine="chat", skill_id=self.skill_id,
                                    skill_revision="forged", output_mode="auto", language="auto")
        with self.assertRaisesRegex(LabelerTaskError, "skill_revision"):
            normalize_config(self.app, raw)

    @staticmethod
    def concurrent_options():
        model = {"model_key": "online:stub", "model_name": "stub-model"}
        return {"concurrency_enabled": True, "workers": [model], "supervisor": model}

    def test_concurrent_skill_retry_preserves_complete_rules_and_fingerprint(self):
        source = self.failed_task(**self.concurrent_options())
        frozen = source["input"]["config"]
        self.assertIn("只描述真实可见的内容", frozen["supervision_rules"])
        self.assertIn("保留手写规则", frozen["supervision_rules"])
        payload, status = self.service.api_retry(source["id"], {"mode": "failed_only"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["task"]["input"]["config"], frozen)

    def test_changed_supervision_rules_reject_retry_without_queueing(self):
        source = self.failed_task(**self.concurrent_options())
        rules = source["input"]["config"]["supervision_rules"]
        with mock.patch("naiba.labeler.jobs.freeze_supervision_rules", return_value=rules + " changed"):
            payload, status = self.service.api_retry(source["id"], {"mode": "failed_only"})
        self.assertEqual(status, 400)
        self.assertIn("监督规则已变化", payload["error"])
        self.assertEqual(self.submit.call_count, 1)

    def test_v1_concurrent_task_requires_explicit_new_batch(self):
        with mock.patch("naiba.labeler.jobs.SUPERVISION_PROMPT_VERSION", "v1"):
            source = self.failed_task(**self.concurrent_options())
        payload, status = self.service.api_retry(source["id"], {"mode": "failed_only"})
        self.assertEqual(status, 400)
        self.assertIn("新建批次", payload["error"])
        self.assertEqual(self.submit.call_count, 1)

    def test_client_cannot_supply_frozen_supervision_rules(self):
        raw = fixtures._base_config(system_prompt="人工规则", output_mode="auto", language="auto",
                                    supervision_rules="forged", **self.concurrent_options())
        with self.assertRaisesRegex(LabelerTaskError, "supervision_rules"):
            normalize_config(self.app, raw)

    def test_supervision_capacity_error_is_reported_before_task_creation(self):
        raw = fixtures._base_config(system_prompt="人工规则", output_mode="auto", language="auto",
                                    **self.concurrent_options())
        with mock.patch("naiba.labeler.jobs.freeze_supervision_rules", side_effect=ValueError("规则超长")):
            payload, status = self.service.api_submit({
                "image_refs": [{"path": str(self.image)}], "config": raw,
            })
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "规则超长")
        self.assertEqual(self.service.store.list_tasks(), [])
        self.assertEqual(self.submit.call_count, 0)

    def test_txt_flag_changes_only_concurrent_result_identity(self):
        raw = fixtures._base_config(system_prompt="人工规则", output_mode="auto", language="auto")
        serial = normalize_config(self.app, raw)
        serial_txt = normalize_config(self.app, {**raw, "write_txt": True})
        self.assertEqual(serial["config_fingerprint"], serial_txt["config_fingerprint"])
        concurrent = normalize_config(self.app, {**raw, **self.concurrent_options()})
        concurrent_txt = normalize_config(self.app, {**raw, **self.concurrent_options(), "write_txt": True})
        self.assertNotEqual(concurrent["config_fingerprint"], concurrent_txt["config_fingerprint"])

    def test_recovered_uncertain_skill_item_requires_confirmation_then_retries(self):
        source = self.failed_task()
        item = self.service.db.list_items(source["id"])[0]
        self.service.db.mark_running(source["id"], item["item_id"])
        self.service.store.update_task(source["id"], status="running")
        self.service.jobs.recover_interrupted()
        _, status = self.service.api_retry(source["id"], {"mode": "unfinished"})
        self.assertEqual(status, 400)
        payload, status = self.service.api_retry(source["id"], {"confirm_uncertain": True})
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["task"]["input"]["config"], source["input"]["config"])


if __name__ == "__main__":
    unittest.main()
