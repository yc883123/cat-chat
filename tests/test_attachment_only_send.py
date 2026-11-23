# -*- coding: utf-8 -*-
"""护栏：纯附件轮次（只发文件/图片、不写文字）发送规格。

保护对象：输入框无文字也能发送附件；模型可见文本口径（compose_user_content）与历史
重放逐字节一致；submit_chat 的放行/拒绝边界；纯附件首轮会话标题回退。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.config import ConfigStore  # noqa: E402
from naiba import net as net_io  # noqa: E402
from naiba.app import NaibaChatApp  # noqa: E402
from naiba.paths import PathContext  # noqa: E402
from naiba.core.attachments import (  # noqa: E402
    ATTACHMENT_ONLY_NOTICE,
    compose_user_content,
    upload_reference_lines,
)
from naiba.run.manager import ConversationRunManager  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402
from tests.test_llm_runtime_retry import FakeResponse  # noqa: E402


class ComposeUserContentTests(unittest.TestCase):
    def test_text_only_passthrough(self):
        self.assertEqual(compose_user_content("你好", []), "你好")

    def test_text_with_uploads_keeps_legacy_join(self):
        uploads = [{"path": "C:/tmp/a.png"}, {"path": "C:/tmp/b.pdf"}]
        legacy = "看看" + "\n" + "\n".join(upload_reference_lines(uploads))
        self.assertEqual(compose_user_content("看看", uploads), legacy)

    def test_attachment_only_gets_notice_line(self):
        uploads = [{"path": "C:/tmp/a.png"}]
        content = compose_user_content("", uploads)
        self.assertEqual(content, ATTACHMENT_ONLY_NOTICE + "\n[用户上传文件：C:/tmp/a.png]")
        self.assertFalse(content.startswith("\n"), "空文字不应留下前导换行")

    def test_blank_text_treated_as_attachment_only(self):
        content = compose_user_content("   \n ", [{"path": "C:/tmp/a.png"}])
        self.assertTrue(content.startswith(ATTACHMENT_ONLY_NOTICE))

    def test_unusable_attachment_entries_ignored(self):
        # 非字典/无 path 的条目一律忽略：既不产出引用行，也不抛异常。
        self.assertEqual(compose_user_content("", [None, "x", {}, {"name": "无路径"}]), "")

    def test_pdf_guidance_dropped_when_pdf_tools_disabled(self):
        """会话工具集不含 read_pdf 时，PDF 引用行不得再指引调用不存在的工具。"""
        uploads = [{"path": "C:/tmp/文档.pdf"}]
        with_guidance = compose_user_content("看看", uploads, pdf_tools=True)
        without = compose_user_content("看看", uploads, pdf_tools=False)
        self.assertIn("read_pdf", with_guidance)
        self.assertNotIn("read_pdf", without)
        self.assertIn("[用户上传文件：C:/tmp/文档.pdf]", without, "引用行本身必须保留")
        self.assertEqual(
            compose_user_content("看看", [{"path": "C:/tmp/a.png"}], pdf_tools=False),
            compose_user_content("看看", [{"path": "C:/tmp/a.png"}], pdf_tools=True),
            "非 PDF 附件不受该开关影响",
        )


class PdfPromptGatingSourceTests(unittest.TestCase):
    """PDF 处理指引必须与工具集同口径（系统提示段 + 附件引用行），不能无条件注入。"""

    def test_run_chat_gates_pdf_prompt_on_allowed_tools(self):
        source = (Path(__file__).resolve().parents[1] / "naiba/run/chat.py").read_text(encoding="utf-8")
        self.assertIn('pdf_tools_enabled = "read_pdf" in', source,
                      "PDF 开关必须按会话固化的工具集判定")
        self.assertIn("if pdf_tools_enabled:", source, "PDF 处理策略段必须条件注入")
        self.assertGreaterEqual(source.count("pdf_tools=pdf_tools_enabled"), 2,
                                "当前轮与历史重放必须同口径")

    def test_history_replay_accepts_pdf_tools_flag(self):
        source = (Path(__file__).resolve().parents[1] / "naiba/core/history.py").read_text(encoding="utf-8")
        self.assertIn("pdf_tools: bool = True", source)
        self.assertIn("pdf_tools=pdf_tools", source)


class _VisionStub:
    def resolve_brain_supports_images(self, profile, probe_if_unknown=False):
        return False


class _CatalogStub:
    def scan(self):
        return {}


class _RegistryStub:
    def schemas(self):
        return []

    def readonly_mcp_tools(self):
        return []


class _SearchStub:
    def is_available(self):
        return False


class SubmitChatAttachmentOnlyTests(unittest.TestCase):
    """submit_chat 边界：文字与可用附件至少有一个（纯附件轮次合法）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.storage = ChatStorage(root / "chat.db")
        app = SimpleNamespace(
            storage=self.storage,
            config=ConfigStore(root / "config.json"),
            catalog=_CatalogStub(),
            vision=_VisionStub(),
            tool_registry=_RegistryStub(),
            web_search=_SearchStub(),
            # submit_chat 的附件落地校验（missing_cache_attachment）需要数据目录。
            paths=SimpleNamespace(data_dir=root / "data"),
        )
        self.manager = ConversationRunManager(app)
        app.config.upsert_model_profile({
            "id": "submit-fixture", "kind": "online", "name": "提交夹具",
            "base_url": "http://127.0.0.1:9/v1", "model": "test-model",
        })
        self.conversation = self.storage.create_conversation(model_name="test-model")

    def tearDown(self):
        self.tmp.cleanup()

    def _submit(self, body):
        # 只验证提交阶段：run 线程不启动（_run_chat 不在本守门范围内）。
        with mock.patch.object(self.manager, "_start", lambda run, target: None):
            return self.manager.submit_chat(body)

    def _user_messages(self):
        conversation = self.storage.get_conversation(self.conversation["id"])
        return [item for item in conversation["messages"] if item["role"] == "user"]

    def test_attachment_only_is_accepted(self):
        run = self._submit(
            {
                "conversation_id": self.conversation["id"],
                "message": "",
                "attachments": [{"name": "照片.png", "path": "C:/tmp/照片.png"}],
            }
        )
        self.assertTrue(run.get("id"), "纯附件轮次应创建 Run")
        messages = self._user_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "", "无文字时用户消息内容保持空串")
        self.assertEqual(messages[0]["metadata"]["attachments"][0]["name"], "照片.png")

    def test_empty_message_and_no_attachments_rejected(self):
        with self.assertRaises(ValueError):
            self._submit({"conversation_id": self.conversation["id"], "message": "", "attachments": []})

    def test_attachment_without_path_rejected(self):
        with self.assertRaises(ValueError):
            self._submit(
                {
                    "conversation_id": self.conversation["id"],
                    "message": "   ",
                    "attachments": [{"name": "只有名字"}],
                }
            )

    def test_attachments_must_be_list(self):
        with self.assertRaises(ValueError):
            self._submit(
                {"conversation_id": self.conversation["id"], "message": "你好", "attachments": {"path": "x"}}
            )

    def test_missing_uploaded_attachment_rejected(self):
        """宿主缓存树内的附件已丢失（被缓存清理）时明确拒绝发送。

        起因（用户报障）：图片被自动清理删掉后消息照发，模型侧只会回"未找到图片文件"，
        用户完全看不出原因；这里必须提前拦下并提示重新上传。
        """
        gone = Path(self.tmp.name) / "data" / "uploads" / "2026-09-13" / "naiba_chat_1_abc_gone.png"
        with self.assertRaises(ValueError) as ctx:
            self._submit(
                {
                    "conversation_id": self.conversation["id"],
                    "message": "看看这张图",
                    "attachments": [{"name": "gone.png", "path": str(gone)}],
                }
            )
        self.assertIn("附件文件已丢失", str(ctx.exception))
        self.assertIn("gone.png", str(ctx.exception))

    def test_external_missing_attachment_still_accepted(self):
        """外部路径（非宿主缓存树）不由宿主判定缺失：仍放行，交给工具层如实报错。"""
        run = self._submit(
            {
                "conversation_id": self.conversation["id"],
                "message": "看看这张图",
                "attachments": [{"name": "外部.png", "path": "C:/tmp/不存在的外部图.png"}],
            }
        )
        self.assertTrue(run.get("id"))

    def test_text_only_still_accepted(self):
        run = self._submit({"conversation_id": self.conversation["id"], "message": "你好"})
        self.assertTrue(run.get("id"))

    def test_unknown_image_capability_is_deferred_without_freezing_fallback(self):
        self.manager.app.vision.brain_image_capability = mock.Mock(
            side_effect=AssertionError("提交阶段不应探测模型")
        )
        run = self._submit({
            "conversation_id": self.conversation["id"], "message": "看看图片",
            "attachments": [{"path": str(Path(self.tmp.name) / "fixture.png")}],
        })
        snapshot = self.storage.get_run_snapshot(run["id"])
        self.assertIsNone(snapshot["chat_supports_images"])
        conversation = self.storage.get_conversation(self.conversation["id"], include_messages=False)
        self.assertEqual(conversation["chat_supports_images"], -1)
        self.manager.app.vision.brain_image_capability.assert_not_called()

    def test_explicit_image_setting_overrides_previous_frozen_capability(self):
        self.storage.set_conversation_chat_supports_images(self.conversation["id"], False)
        self.manager.app.config.upsert_model_profile({
            "id": "submit-fixture", "kind": "online", "name": "提交夹具",
            "base_url": "http://127.0.0.1:9/v1", "model": "test-model",
            "supports_images": True,
        })
        run = self._submit({"conversation_id": self.conversation["id"], "message": "你好"})
        self.assertTrue(self.storage.get_run_snapshot(run["id"])["chat_supports_images"])

    def test_attachment_only_first_turn_title_fallback(self):
        self._submit(
            {
                "conversation_id": self.conversation["id"],
                "message": "",
                "attachments": [{"name": "照片.png", "path": "C:/tmp/照片.png"}],
            }
        )
        conversation = self.storage.get_conversation(self.conversation["id"])
        self.assertEqual(conversation["title"], "照片.png", "无文字首轮用附件名作标题")

    def test_text_turn_title_still_from_text(self):
        self._submit(
            {
                "conversation_id": self.conversation["id"],
                "message": "帮我看看这张图",
                "attachments": [{"name": "照片.png", "path": "C:/tmp/照片.png"}],
            }
        )
        conversation = self.storage.get_conversation(self.conversation["id"])
        self.assertEqual(conversation["title"], "帮我看看这张图")


class ImageCapabilityWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        paths = PathContext.local(root, root / "config.json")
        config = ConfigStore(paths.config_path, paths=paths)
        config.data.update({
            "workspace_dir": str(root / "work"), "mcp_servers": [],
            "auto_compaction": {"mode": "off"},
            "providers": [{
                "id": "images", "kind": "online", "name": "Images fixture", "api_key": "",
                "base_url": "http://127.0.0.1:1/v1", "request_format": "openai_chat",
                "model": "unknown-alias", "context_window": 32000, "max_output_tokens": 2048,
            }],
        })
        config.save()
        self.enterContext(mock.patch.dict(os.environ, {"NAIBA_ERROR_DUMP_DIR": str(root / "errors")}))
        self.wire = []
        self.enterContext(mock.patch.object(net_io, "open", side_effect=self._transport))
        self.app = NaibaChatApp(paths=paths)
        self.addCleanup(self.app.stop)
        self.cid = self.app.storage.create_conversation(
            model_key="online:images", model_name="unknown-alias", reasoning_effort="off",
        )["id"]
        self.app.storage.set_enabled_tool_ids(self.cid, ["vision_analyze"])
        self.app.storage.update_conversation_settings(self.cid, stream_enabled=False)
        self.probe = self.enterContext(mock.patch.object(
            self.app.vision, "_probe_image_capability", return_value=None,
        ))
        from PIL import Image

        self.image_path = root / "fixture.png"
        Image.new("RGB", (32, 32), "blue").save(self.image_path)

    def _transport(self, request, **kwargs):
        self.assertEqual(request.full_url, "http://127.0.0.1:1/v1/chat/completions")
        self.wire.append(json.loads(request.data))
        return FakeResponse({
            "choices": [{"message": {"role": "assistant", "content": "已处理附件"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    def _send(self, *, image=False):
        run = self.app.runs.submit_chat({
            "conversation_id": self.cid, "message": "请看看附件" if image else "你好",
            "attachments": [{"path": str(self.image_path)}] if image else [],
        })
        deadline = time.monotonic() + 10
        while run["id"] in self.app.runs._threads and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotIn(run["id"], self.app.runs._threads)
        record = self.app.storage.get_background_task(run["id"])
        self.assertEqual(record["status"], "completed", record.get("error"))

    def _saved_capability(self):
        return self.app.storage.get_conversation(self.cid, include_messages=False)["chat_supports_images"]

    def test_text_turn_defers_probe_and_unknown_image_result_is_retried(self):
        self._send()
        self.probe.assert_not_called()
        self.assertEqual(self._saved_capability(), -1)
        self._send(image=True)
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(self._saved_capability(), -1)
        self.probe.return_value = True
        self._send(image=True)
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual(self._saved_capability(), 1)
        images = [
            part for message in self.wire[-1]["messages"]
            if isinstance(message["content"], list)
            for part in message["content"] if part.get("type") == "image_url"
        ]
        self.assertTrue(images)

    def test_explicit_false_does_not_persist_when_setting_returns_to_auto(self):
        self.app.config.data["providers"][0]["supports_images"] = False
        self._send(image=True)
        self.probe.assert_not_called()
        self.assertEqual(self._saved_capability(), -1)
        self.app.config.data["providers"][0].pop("supports_images")
        self.probe.return_value = True
        self._send(image=True)
        self.probe.assert_called_once()
        self.assertEqual(self._saved_capability(), 1)

    def test_capability_cache_write_failure_keeps_chat_usable(self):
        self.probe.return_value = True
        with mock.patch.object(
            self.app.storage, "set_conversation_chat_supports_images", side_effect=OSError("fixture write failure"),
        ):
            self._send(image=True)
        self.assertEqual(self._saved_capability(), -1)


if __name__ == "__main__":
    unittest.main()
