# -*- coding: utf-8 -*-
"""会话模型必须来自设置页检测目录；供应商默认模型不再作为普通对话回退。

会话下拉只显示设置页检查返回的模型目录。旧会话中的模型可以显示，但若不在最新目录中，
发送前必须重新检查供应商模型。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.config import ConfigStore  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


class ComposerModelFollowMarkupTests(unittest.TestCase):
    """前端守卫：会话下拉只使用检测目录，提交/保存都用原始选择。"""

    def _models(self) -> str:
        return (ROOT / "public/js/07-models-agents.js").read_text(encoding="utf-8")

    def _stream(self) -> str:
        return (ROOT / "public/js/11-run-stream.js").read_text(encoding="utf-8")

    def test_composer_uses_catalog_and_marks_stale_saved_model(self) -> None:
        models = self._models()
        self.assertNotIn("FOLLOW_PROVIDER_MODEL", models)
        self.assertIn("state.providerModelCatalogs[provider?.model_key]", models)
        self.assertIn("请重新检查模型", models)
        self.assertIn("composerModelIsValidated", models)

    def test_choice_is_raw_selection_not_resolved_model(self) -> None:
        models = self._models()
        self.assertIn("export function composerModelChoice()", models)
        self.assertNotIn("selectedModelName", models, "旧的「解析后模型名」入口必须退役")
        self.assertIn("persistConversationModelName(composerModelChoice())", models)

    def test_submit_and_new_conversation_send_raw_choice(self) -> None:
        self.assertIn("model_name: composerModelChoice()", self._stream())
        conversations = (ROOT / "public/js/08-conversations.js").read_text(encoding="utf-8")
        self.assertIn("resolveNewConversationModel()", conversations)
        self.assertIn("model_name: model.model_name", conversations)

    def test_catalog_cache_needs_models_not_a_lookup(self) -> None:
        """空目录（供应商 200 但没模型）不算已缓存，否则本页会永久卡在「请先在设置中检查模型」。"""
        models = self._models()
        self.assertIn("(state.providerModelCatalogs[providerKey] || []).length", models)

    def test_dropdown_offers_refresh_without_persisting_it(self) -> None:
        """会话内可直接重拉目录，但「↻ 重新检测模型」只是动作，绝不落库、绝不发给模型。"""
        models = self._models()
        self.assertIn("const COMPOSER_MODEL_REFRESH = '__refresh_composer_models__'", models)
        self.assertIn("↻ 重新检测模型", models)
        self.assertIn("if (select.value === COMPOSER_MODEL_REFRESH)", models)
        self.assertIn("return value === COMPOSER_MODEL_REFRESH ? '' : value;", models)
        self.assertIn("{ force = false } = {}", models, "必须保留「强制重拉」通道")

    def test_new_conversation_never_auto_picks_first_entry(self) -> None:
        """目录顺序由供应商返回顺序决定、没有任何语义，不得静默替用户选第一项。"""
        models = self._models()
        self.assertNotIn("select.value = entries[0].id", models)
        self.assertIn("请选择模型…", models)

    def test_provider_save_routes_through_app(self) -> None:
        http = (ROOT / "naiba/http.py").read_text(encoding="utf-8")
        self.assertIn("self.app.api_upsert_model_profile(body)", http)
        self.assertNotIn("self.app.config.upsert_provider(body)", http)
        self.assertNotIn("self.app.config.upsert_model_profile(body)", http)


class ClearConversationModelOverrideTests(unittest.TestCase):
    """`clear_conversation_model_overrides` 的定向清理语义。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _conversation(self, model_key: str, model_name: str, provider_id: str = "") -> dict:
        return self.storage.create_conversation(
            model_key=model_key, model_name=model_name, provider_id=provider_id
        )

    def _model_name(self, conversation_id: str) -> str:
        row = self.storage.get_conversation(conversation_id, include_messages=False)
        return str(row["model_name"] or "")

    def test_only_implicit_followers_are_cleared(self) -> None:
        implicit = self._conversation("online:ted", "deepseek-v4.1-flash")
        pinned = self._conversation("online:ted", "deepseek-chat")
        other_api = self._conversation("online:other", "deepseek-v4.1-flash")
        following = self._conversation("online:ted", "")
        legacy = self._conversation("", "deepseek-v4.1-flash", provider_id="ted")
        # create_conversation 在 model_key 为空但给了 provider_id 时会补成 online:<id>，
        # 这里显式清回空串，模拟 v17 之前只留 provider_id 的旧会话。
        self.storage.update_conversation_settings(legacy["id"], model_key="")

        cleared = self.storage.clear_conversation_model_overrides(
            "online:ted", "deepseek-v4.1-flash"
        )

        self.assertEqual(cleared, 2)
        self.assertEqual(self._model_name(implicit["id"]), "")
        self.assertEqual(self._model_name(legacy["id"]), "")
        self.assertEqual(self._model_name(pinned["id"]), "deepseek-chat")
        self.assertEqual(self._model_name(other_api["id"]), "deepseek-v4.1-flash")
        self.assertEqual(self._model_name(following["id"]), "")

    def test_silent_fix_keeps_updated_at_and_resets_image_capability(self) -> None:
        conversation = self._conversation("online:ted", "old-model")
        before = self.storage.get_conversation(conversation["id"], include_messages=False)
        self.storage.set_conversation_chat_supports_images(conversation["id"], True)

        self.storage.clear_conversation_model_overrides("online:ted", "old-model")

        after = self.storage.get_conversation(conversation["id"], include_messages=False)
        self.assertEqual(after["updated_at"], before["updated_at"], "静默修正不得重排侧栏")
        self.assertEqual(after["chat_supports_images"], -1, "模型变了，图像能力需重新探测")

    def test_blank_arguments_are_noop(self) -> None:
        conversation = self._conversation("online:ted", "old-model")
        self.assertEqual(self.storage.clear_conversation_model_overrides("", "old-model"), 0)
        self.assertEqual(self.storage.clear_conversation_model_overrides("online:ted", ""), 0)
        self.assertEqual(self._model_name(conversation["id"]), "old-model")


class ApiUpsertModelProfileTests(unittest.TestCase):
    """供应商默认模型变更不应改写会话模型选择。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.storage = ChatStorage(root / "chat.db")
        self.config = ConfigStore(root / "config.json")
        self.app = SimpleNamespace(config=self.config, storage=self.storage)
        provider = self._save_provider("deepseek-v4.1-flash")
        self.conversation = self.storage.create_conversation(
            model_key=provider["model_key"], model_name="deepseek-v4.1-flash"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _save_provider(self, model: str) -> dict:
        from naiba.app import NaibaChatApp

        return NaibaChatApp.api_upsert_model_profile(
            self.app,
            {
                "id": "ted",
                "kind": "online",
                "name": "teds",
                "base_url": "https://example.com/v1",
                "api_key": "sk-test",
                "request_format": "openai_chat",
                "model": model,
            },
        )

    def _model_name(self) -> str:
        row = self.storage.get_conversation(self.conversation["id"], include_messages=False)
        return str(row["model_name"] or "")

    def test_default_model_change_does_not_control_conversation(self) -> None:
        saved = self._save_provider("deepseek-v4-pro")
        self.assertNotIn("cleared_model_overrides", saved)
        self.assertEqual(self._model_name(), "deepseek-v4.1-flash")

    def test_same_default_model_keeps_conversation(self) -> None:
        saved = self._save_provider("deepseek-v4.1-flash")
        self.assertNotIn("cleared_model_overrides", saved)
        self.assertEqual(self._model_name(), "deepseek-v4.1-flash")

    def test_explicit_other_model_is_kept(self) -> None:
        self.storage.update_conversation_settings(self.conversation["id"], model_name="deepseek-chat")
        self._save_provider("deepseek-v4-pro")
        self.assertEqual(self._model_name(), "deepseek-chat")


if __name__ == "__main__":
    unittest.main()
