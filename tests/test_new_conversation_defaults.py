"""新对话默认独立配置；发送时将解析后的真实 API 绑定固化到会话和 Run。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from naiba.config import ConfigStore
from naiba.run.manager import ConversationRunManager
from naiba.storage.store import ChatStorage


PAIR = ("new_conversation_model_key", "new_conversation_model_name")


def add_profiles(config):
    for pid, kind in (("alpha", "online"), ("beta", "online"), ("local-one", "local")):
        config.upsert_model_profile({"id": pid, "kind": kind, "name": pid,
                                     "base_url": "http://127.0.0.1:9/v1", "model": "profile-model",
                                     "request_format": "openai_chat", "local_backend": "ollama",
                                     "context_window": 8192})


class NewConversationDefaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.json"
        self.config = ConfigStore(self.path)
        add_profiles(self.config)

    def selection(self, key="online:beta", name="selected-model"):
        return dict(zip(PAIR, (key, name)))

    def test_defaults_are_unset_without_changing_shared_fallback(self):
        for key in PAIR:
            self.assertEqual(self.config.public().get(key), "")
        self.assertEqual(self.config.default_model_key(), "online:alpha")
        reloaded = ConfigStore(self.path)
        self.assertEqual(reloaded.default_model_key(), "online:alpha")
        self.assertEqual([reloaded.public().get(key) for key in PAIR], ["", ""])

    def test_save_pair_and_shared_default_in_one_disk_write(self):
        save = self.config.save
        with mock.patch.object(self.config, "save", wraps=save) as saved:
            public = self.config.update_settings(self.selection())
        self.assertEqual(saved.call_count, 1)
        self.assertEqual({key: public[key] for key in PAIR}, self.selection())
        self.assertEqual(self.config.default_model_key(), "online:beta")
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual({key: persisted[key] for key in PAIR}, self.selection())
        self.assertEqual(persisted["default_model_key"], "online:beta")
        reloaded = ConfigStore(self.path)
        self.assertEqual({key: reloaded.public()[key] for key in PAIR}, self.selection())

    def test_clear_pair_keeps_shared_default(self):
        self.config.update_settings(self.selection())
        self.config.update_settings(self.selection("", ""))
        self.assertEqual([self.config.public()[key] for key in PAIR], ["", ""])
        self.assertEqual(self.config.default_model_key(), "online:beta")

    def test_invalid_pair_never_partially_changes_configuration(self):
        invalid = [
            {PAIR[0]: "online:beta"}, {PAIR[1]: "model"},
            self.selection("online:beta", ""), self.selection("", "model"),
            self.selection("online:missing"), self.selection("local:alpha"),
            self.selection(None), self.selection("online:beta", False),
            self.selection("online:beta", "m" * 257),
            {**self.selection(), "default_model_key": "online:alpha"},
        ]
        for body in invalid:
            with self.subTest(body=body):
                before, disk = copy.deepcopy(self.config.data), self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.config.update_settings(body)
                self.assertEqual(self.config.data, before)
                self.assertEqual(self.path.read_bytes(), disk)

    def test_failed_save_restores_in_memory_pair_and_shared_default(self):
        before, disk = copy.deepcopy(self.config.data), self.path.read_bytes()
        with mock.patch("naiba.config.os.replace", side_effect=OSError("配置写入失败")):
            with self.assertRaisesRegex(OSError, "配置写入失败"):
                self.config.update_settings(self.selection())
        self.assertEqual(self.config.data, before)
        self.assertEqual(self.path.read_bytes(), disk)

    def test_invalid_other_setting_does_not_publish_pair(self):
        before = copy.deepcopy(self.config.data)
        with self.assertRaises(ValueError):
            self.config.update_settings({**self.selection(), "host": "invalid-host"})
        self.assertEqual(self.config.data, before)

    def test_deleted_profile_keeps_identifiable_default_for_invalid_ui(self):
        self.config.update_settings(self.selection("local:local-one"))
        self.config.delete_model_profile("local:local-one")
        reloaded = ConfigStore(self.path)
        self.assertEqual(reloaded.public()[PAIR[0]], "local:local-one")
        self.assertEqual(reloaded.public()[PAIR[1]], "selected-model")
        self.assertEqual(reloaded.default_model_key(), "online:alpha")

    def test_runtime_settings_response_exposes_atomic_saved_pair(self):
        from naiba.app import NaibaChatApp
        from naiba.core.contracts import NEW_CONVERSATION_DEFAULT_FIELDS, NewConversationDefaultSettings

        self.assertEqual(tuple(NEW_CONVERSATION_DEFAULT_FIELDS), PAIR)
        self.assertEqual(set(NewConversationDefaultSettings.__annotations__), set(PAIR))
        app = SimpleNamespace(
            config=self.config, paths=SimpleNamespace(data_dir=self.config.resolve_data_dir()),
            updater=SimpleNamespace(configure_proxy=lambda _: None), listener_host="127.0.0.1",
            executor=SimpleNamespace(set_permission_mode=lambda _: None),
            _cache_bytes_payload=lambda: {},
        )
        with mock.patch("naiba.app.net_io.configure"), mock.patch("naiba.app.net_io.proxy_state", return_value={}):
            response, status = NaibaChatApp.api_update_runtime_settings(app, self.selection())
        self.assertEqual(status, 200)
        self.assertEqual({key: response["settings"][key] for key in PAIR}, self.selection())
        self.assertEqual(response["default_model_key"], "online:beta")
        self.assertEqual(response["settings"]["default_model_key"], "online:beta")


class SubmittedConversationBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = ConfigStore(self.root / "config.json")
        add_profiles(self.config)
        self.storage = ChatStorage(self.root / "chat.db")
        self.app = SimpleNamespace(
            storage=self.storage, config=self.config,
            catalog=SimpleNamespace(scan=lambda: {}),
            vision=SimpleNamespace(resolve_brain_supports_images=lambda *a, **kw: False),
            tool_registry=SimpleNamespace(schemas=lambda: [], readonly_mcp_tools=lambda: []),
            web_search=SimpleNamespace(is_available=lambda: False), paths=SimpleNamespace(data_dir=self.root),
        )
        self.manager = ConversationRunManager(self.app)

    def conversation(self, *, key="", provider=""):
        row = self.storage.create_conversation(model_name="chosen-model")
        # 模拟升级前记录：create_conversation 会自动补 online:<provider_id>。
        with self.storage._connect() as db:
            db.execute("UPDATE conversations SET model_key=?, provider_id=? WHERE id=?", (key, provider, row["id"]))
        return row["id"]

    def submit(self, cid, **kwargs):
        with mock.patch.object(self.manager, "_start"):
            return self.manager.submit_chat({"conversation_id": cid, "message": "检查绑定"}, **kwargs)

    def test_empty_binding_is_frozen_to_actual_default_before_run(self):
        cid = self.conversation()
        run = self.submit(cid)
        self.assertEqual(self.storage.get_conversation(cid)["model_key"], "online:alpha")
        self.assertEqual(self.storage.get_run_snapshot(run["id"])["model_key"], "online:alpha")
        self.config.set_default_model_key("online:beta")
        self.assertEqual(self.storage.get_conversation(cid)["model_key"], "online:alpha")
        self.assertEqual(self.storage.get_run_snapshot(run["id"])["model_key"], "online:alpha")

    def test_legacy_local_provider_is_resolved_without_online_prefix(self):
        cid = self.conversation(provider="local-one")
        run = self.submit(cid)
        self.assertEqual(self.storage.get_conversation(cid)["model_key"], "local:local-one")
        self.assertEqual(self.storage.get_run_snapshot(run["id"])["model_key"], "local:local-one")

    def test_missing_or_ambiguous_legacy_binding_is_rejected_without_writes(self):
        for variant in ("missing", "ambiguous", "deleted-key"):
            with self.subTest(variant=variant):
                cid = self.conversation(key="online:gone" if variant == "deleted-key" else "",
                                        provider="alpha" if variant == "ambiguous" else "gone")
                before = self.storage.get_conversation(cid)
                profiles = copy.deepcopy(self.config.data["providers"])
                if variant == "ambiguous":
                    self.config.data["providers"].append({**profiles[0], "kind": "local"})
                try:
                    with self.assertRaises(ValueError):
                        self.submit(cid)
                    self.assertEqual(self.storage.get_conversation(cid), before)
                    self.assertIsNone(self.storage.active_run(cid))
                finally:
                    self.config.data["providers"] = profiles

    def test_opening_history_remains_read_only(self):
        cid = self.conversation(provider="local-one")
        self.storage.get_conversation(cid)
        self.assertEqual(self.storage.get_conversation(cid)["model_key"], "")

    def test_no_profile_configuration_fails_before_message_or_run_is_created(self):
        self.config.data["providers"] = []
        self.config.data["default_model_key"] = ""
        cid = self.conversation()
        with self.assertRaisesRegex(ValueError, "未选择模型配置"):
            self.submit(cid)
        self.assertEqual(self.storage.get_conversation(cid)["messages"], [])
        self.assertIsNone(self.storage.active_run(cid))

    def test_task_override_uses_its_profile_without_changing_conversation_binding(self):
        cid = self.conversation(key="online:alpha")
        with mock.patch.object(self.manager, "_start"):
            run = self.manager.submit_chat({"conversation_id": cid, "message": "独立任务",
                                            "model_key": "local:local-one", "model_name": "task-model"},
                                           task_model_override=True)
        conversation = self.storage.get_conversation(cid)
        snapshot = self.storage.get_run_snapshot(run["id"])
        self.assertEqual((conversation["model_key"], conversation["model_name"]), ("online:alpha", "chosen-model"))
        self.assertEqual((snapshot["model_key"], snapshot["model_name"]), ("local:local-one", "task-model"))


if __name__ == "__main__":
    unittest.main()
