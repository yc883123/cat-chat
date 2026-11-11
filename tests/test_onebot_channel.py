# -*- coding: utf-8 -*-
"""守门：QQ 渠道配置、持久状态与适配器（NapCatQQ / OneBot v11）。

对应计划 §七 的全部单元覆盖项：
- 配置迁移、口令脱敏、启用前校验；
- 渠道账本：绑定、去重、投递分段、确认凭据的时效与一次性；
- 适配器：白名单、群聊默认拒绝、绑定缺失、活动 Run 冲突、完整回复读取、
  长文本分段、投递失败状态、重启补投递；
- 反向 WebSocket 鉴权（路径 + 口令）与 lifecycle。

全部使用本地假 NapCat 与假模型，不访问真实 QQ 或模型供应商。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.channels import store as channel_store  # noqa: E402
from naiba.channels.adapter import OneBotAdapter, ParsedMessage  # noqa: E402
from naiba.config import ConfigStore, normalize_onebot, onebot_public  # noqa: E402
from naiba.paths import PathContext  # noqa: E402


def private_message(text="你好", *, message_id="1", user_id="20000", account_id="10000", media=None):
    return ParsedMessage(
        account_id=account_id,
        message_id=message_id,
        scope="private",
        qq_user_id=user_id,
        qq_group_id="",
        text=text,
        media=list(media or []),
    )


class _FakeSender:
    """假 NapCat：记录所有动作，可配置失败。"""

    def __init__(self, *, fail_actions=frozenset()):
        self.calls: list[tuple[str, dict]] = []
        self.fail_actions = set(fail_actions)
        self._lock = threading.Lock()

    def call_action(self, action, params):
        with self._lock:
            self.calls.append((action, dict(params)))
        if action in self.fail_actions:
            return None
        return {"status": "ok", "retcode": 0, "data": {}}

    def sent_texts(self):
        return [params.get("message") for action, params in self.calls if action.startswith("send_")]


class _FakeRuns:
    """最小 Run 管理器：submit_chat 直接给一个 run_id，get 返回终态。"""

    TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
    ACTIVE = {"queued", "running", "waiting", "cancelling", "stopping"}

    def __init__(self, app):
        self.app = app
        self.submitted: list[dict] = []
        self.runs: dict[str, dict] = {}
        self.confirm_calls: list[tuple] = []
        self.reject_calls: list[tuple] = []
        self.snapshot: list[dict] = []
        self.submit_error: Exception | None = None
        self.reply_text = "这是完整回复。"
        self._next = 0
        self._lock = threading.Lock()

    def submit_chat(self, body):
        if self.submit_error is not None:
            raise self.submit_error
        with self._lock:
            self._next += 1
            run_id = f"run-{self._next}"
        self.submitted.append(body)
        self.runs[run_id] = {
            "id": run_id,
            "conversation_id": body.get("conversation_id"),
            "status": "completed",
            "error": "",
            "detail": {"message": "任务已完成"},
        }
        # 假模型：立刻落一条完整的 assistant 消息（含 run_id），模拟落库后的终态。
        self.app.storage.add_message(
            str(body.get("conversation_id") or ""), "assistant", self.reply_text,
            {"run_id": run_id},
        )
        return self.runs[run_id]

    def get(self, run_id):
        return self.runs.get(run_id)

    def confirmation_snapshot(self, run_id):
        return list(self.snapshot)

    def confirm_tool_async(self, run_id, confirm_id, allow_run=None):
        self.confirm_calls.append((run_id, confirm_id, allow_run))
        return (True, "已执行")

    def reject_tool(self, run_id, confirm_id):
        self.reject_calls.append((run_id, confirm_id))
        return (True, "已拒绝")


class _FakeConfig:
    def __init__(self, onebot):
        self.data = {"imaging": {}, "onebot": onebot, "access_token": "global-token"}
        self.default_model_key_value = ""

    def onebot_config(self):
        return normalize_onebot(self.data.get("onebot"))

    def default_model_key(self):
        return self.default_model_key_value

    def model_profiles(self):
        return []


class _FakeApp:
    def __init__(self, root: Path):
        self.paths = PathContext.local(root, root / "config.json")
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        from naiba.storage.store import ChatStorage

        self.storage = ChatStorage(self.paths.data_dir / "chat.db")
        self.config = _FakeConfig({
            "enabled": True,
            "access_token": "onebot-secret",
            "allowed_user_ids": ["20000"],
            "allow_groups": False,
        })
        self.runs = _FakeRuns(self)
        self.listener_host = "0.0.0.0"

    def new_conversation(self, title="QQ 会话"):
        return self.storage.create_conversation(
            title=title, provider_id="", agent_id="", model_key="online:x",
            model_name="fake-model",
        )


def _force_expire(store, confirm_id: str) -> None:
    """把凭据的过期时间推到过去（测试时效路径；显式关闭连接，Windows 上才删得掉临时目录）。"""
    import sqlite3

    connection = sqlite3.connect(store.path)
    try:
        with connection:
            connection.execute(
                "UPDATE channel_confirmations SET expires_at=? WHERE confirm_id=?",
                (channel_store.now_ms() - 1000, confirm_id),
            )
    finally:
        connection.close()


class OnebotConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_default_config_has_onebot_block(self):
        store = ConfigStore(self.root / "config.json")
        block = store.data["onebot"]
        self.assertFalse(block["enabled"])
        self.assertEqual(block["listen_host"], "127.0.0.1")
        self.assertEqual(block["listen_port"], 8766)
        self.assertEqual(block["path"] / 1 if False else block["path"], "/onebot/v11/ws")
        self.assertEqual(block["allowed_user_ids"], [])
        self.assertFalse(block["allow_groups"])

    def test_legacy_config_without_onebot_is_migrated(self):
        payload = {"host": "127.0.0.1", "port": 8765, "access_token": "1234"}
        (self.root / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        store = ConfigStore(self.root / "config.json")
        self.assertEqual(store.data["onebot"]["listen_port"], 8766)

    def test_partial_onebot_block_is_merged_with_defaults(self):
        payload = {"onebot": {"enabled": False, "listen_port": 9000}}
        (self.root / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        store = ConfigStore(self.root / "config.json")
        block = store.data["onebot"]
        self.assertEqual(block["listen_port"], 9000)
        self.assertEqual(block["path"], "/onebot/v11/ws")
        self.assertFalse(block["allow_groups"])

    def test_dirty_values_are_normalized_not_fatal(self):
        normalized = normalize_onebot({
            "listen_port": "not-a-port",
            "path": "onebot/ws",
            "allowed_user_ids": ["1", 2, {"x": 1}, ""],
            "enable": "yes",
        })
        self.assertEqual(normalized["listen_port"], 8766)
        self.assertEqual(normalized["path"], "/onebot/ws")
        self.assertEqual(normalized["allowed_user_ids"], ["1", "2"])
        self.assertFalse(normalized["enabled"])

    def test_public_snapshot_never_exposes_token(self):
        public = onebot_public({"access_token": "super-secret", "enabled": True})
        self.assertNotIn("access_token", public)
        self.assertTrue(public["has_access_token"])
        # 整个 settings 载荷里也不能出现口令明文。
        store = ConfigStore(self.root / "config.json")
        store.update_onebot({"access_token": "super-secret", "allowed_user_ids": ["20000"]})
        payload = json.dumps(store.public(), ensure_ascii=False)
        self.assertNotIn("super-secret", payload)
        self.assertNotIn("onebot-secret", payload)

    def test_enable_requires_token_and_allow_list(self):
        store = ConfigStore(self.root / "config.json")
        with self.assertRaises(ValueError):
            store.update_onebot({"enabled": True, "allowed_user_ids": ["20000"]})
        with self.assertRaises(ValueError):
            store.update_onebot({"enabled": True, "access_token": "abcd1234"})
        store.update_onebot({"enabled": True, "access_token": "abcd1234", "allowed_user_ids": ["20000"]})
        self.assertTrue(store.data["onebot"]["enabled"])

    def test_allow_groups_requires_group_allow_list(self):
        store = ConfigStore(self.root / "config.json")
        with self.assertRaises(ValueError):
            store.update_onebot({"allow_groups": True})
        store.update_onebot({"allow_groups": True, "allowed_group_ids": ["777"]})
        self.assertIn("777", store.data["onebot"]["allowed_group_ids"])

    def test_blank_token_on_update_preserves_existing(self):
        store = ConfigStore(self.root / "config.json")
        store.update_onebot({"access_token": "keep-me-please"})
        store.update_onebot({"allowed_user_ids": ["20000"], "access_token": ""})
        self.assertEqual(store.data["onebot"]["access_token"], "keep-me-please")
        store.update_onebot({"clear_access_token": True})
        self.assertEqual(store.data["onebot"]["access_token"], "")

    def test_global_settings_update_routes_onebot(self):
        store = ConfigStore(self.root / "config.json")
        result = store.update_settings({"onebot": {
            "access_token": "abcd1234", "allowed_user_ids": ["20000"],
        }})
        self.assertTrue(result["onebot"]["has_access_token"])
        self.assertNotIn("access_token", result["onebot"])


class ChannelStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.store = channel_store.ChannelStore(self.root / "channels" / "qq.db")

    def test_binding_is_scoped_by_account_scope_and_group(self):
        self.store.bind("10000", "private", "20000", "conv-a")
        self.store.bind("10001", "private", "20000", "conv-b")
        self.assertEqual(
            self.store.binding_for("10000", "private", "20000")["conversation_id"], "conv-a"
        )
        self.assertEqual(
            self.store.binding_for("10001", "private", "20000")["conversation_id"], "conv-b"
        )
        # 不同群不共用上下文。
        self.assertIsNone(self.store.binding_for("10000", "group", "20000", "777"))

    def test_rebind_replaces_conversation(self):
        self.store.bind("10000", "private", "20000", "conv-a")
        self.store.bind("10000", "private", "20000", "conv-b")
        self.assertEqual(
            self.store.binding_for("10000", "private", "20000")["conversation_id"], "conv-b"
        )
        self.assertTrue(self.store.unbind("10000", "private", "20000"))
        self.assertIsNone(self.store.binding_for("10000", "private", "20000"))

    def test_duplicate_message_id_is_detected(self):
        _, created = self.store.record_message(
            "10000", "m1", scope="private", qq_user_id="20000"
        )
        self.assertTrue(created)
        _, created_again = self.store.record_message(
            "10000", "m1", scope="private", qq_user_id="20000"
        )
        self.assertFalse(created_again)

    def test_deliveries_are_idempotent_and_resumable(self):
        segments = ["第一段", "第二段", "第三段"]
        first = self.store.prepare_deliveries(
            "10000", "m1", run_id="r1", conversation_id="c1",
            scope="private", qq_user_id="20000", qq_group_id="", segments=segments,
        )
        self.assertEqual(len(first), 3)
        # 重复登记不新建（重连后重跑同一段回复不会双发）。
        again = self.store.prepare_deliveries(
            "10000", "m1", run_id="r1", conversation_id="c1",
            scope="private", qq_user_id="20000", qq_group_id="", segments=segments,
        )
        self.assertEqual(len(again), 3)
        self.store.mark_delivery(first[0]["delivery_id"], channel_store.DELIVERY_SENT)
        self.store.mark_delivery(first[1]["delivery_id"], channel_store.DELIVERY_FAILED, error="net")
        unfinished = self.store.unfinished_deliveries("10000")
        self.assertEqual(
            sorted(item["segment_index"] for item in unfinished), [1, 2]
        )

    def test_confirmation_is_single_use_and_expires(self):
        record = self.store.register_confirmation(
            "10000", "cf-1", run_id="r1", conversation_id="c1",
            qq_user_id="20000", qq_group_id="", tool="write_file", ttl_seconds=60,
        )
        self.assertEqual(record["status"], channel_store.CONFIRM_PENDING)
        self.assertTrue(self.store.resolve_confirmation("10000", "cf-1", channel_store.CONFIRM_APPROVED))
        # 再批一次必须失败（重放批准无效）。
        self.assertFalse(self.store.resolve_confirmation("10000", "cf-1", channel_store.CONFIRM_APPROVED))

    def test_expired_confirmation_cannot_be_resolved(self):
        self.store.register_confirmation(
            "10000", "cf-2", run_id="r1", conversation_id="c1",
            qq_user_id="20000", qq_group_id="", tool="pwsh", ttl_seconds=0,
        )
        # ttl_seconds 被夹到至少 1 秒：等它自然过期既不现实也不稳定，直接改库。
        _force_expire(self.store, "cf-2")
        self.store.expire_stale_confirmations()
        record = self.store.get_confirmation("10000", "cf-2")
        self.assertEqual(record["status"], channel_store.CONFIRM_EXPIRED)
        self.assertFalse(self.store.resolve_confirmation("10000", "cf-2", channel_store.CONFIRM_APPROVED))

    def test_void_on_restart_prevents_revival(self):
        self.store.register_confirmation(
            "10000", "cf-3", run_id="r1", conversation_id="c1",
            qq_user_id="20000", qq_group_id="", tool="pwsh", ttl_seconds=600,
        )
        self.assertEqual(self.store.void_all_confirmations(), 1)
        self.assertEqual(
            self.store.get_confirmation("10000", "cf-3")["status"], channel_store.CONFIRM_VOID
        )


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.app = _FakeApp(self.root)
        self.store = channel_store.ChannelStore(self.root / "data" / "channels" / "qq.db")
        self.sender = _FakeSender()
        self.adapter = OneBotAdapter(self.app, self.store, self.sender)
        self.conversation = self.app.new_conversation()
        self.conversation_id = str(self.conversation["id"])
        self.addCleanup(self.adapter.stop, 1.0)

    def _bind(self):
        self.store.bind("10000", "private", "20000", self.conversation_id)

    def _event(self, text="你好", message_id="m1", user_id="20000", extra=None):
        message = [{"type": "text", "data": {"text": text}}] + list(extra or [])
        return {
            "post_type": "message",
            "message_type": "private",
            "self_id": 10000,
            "message_id": message_id,
            "user_id": user_id,
            "message": message,
        }

    def _process(self, message):
        """同步跑完整条入站链路（跳过线程，便于断言）。"""
        record, created = self.store.record_message(
            message.account_id, message.message_id,
            scope=message.scope, qq_user_id=message.qq_user_id,
            qq_group_id=message.qq_group_id,
        )
        self.assertTrue(created)
        self.adapter._process(message, record)

    def _wait_for_adapter(self, timeout=30.0):
        """等真实处理线程完成；拒绝状态先落库，不能代替后续发送完成。"""
        deadline = time.monotonic() + timeout
        while True:
            with self.adapter._threads_lock:
                workers = list(self.adapter._threads.values())
            if not workers:
                return
            for worker in workers:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
            self.assertEqual(
                [worker.name for worker in workers if worker.is_alive()], [],
                "OneBot 消息处理线程未在期限内完成",
            )

    def test_unauthorized_user_is_rejected_without_run(self):
        self._bind()
        event = self._event(user_id="99999")
        self.adapter.handle_event(event)
        self._wait_for_adapter()
        self.assertEqual(self.app.runs.submitted, [])
        self.assertEqual(self.sender.sent_texts(), ["这个 QQ 号还没有被允许使用 Cat Chat。"])
        record = self.store.lookup_message("10000", "m1")
        self.assertEqual(record["status"], channel_store.MESSAGE_REJECTED)

    def test_group_message_default_denied(self):
        self._bind()
        event = self._event()
        event["message_type"] = "group"
        event["group_id"] = 777
        self.adapter.handle_event(event)
        self._wait_for_adapter()
        self.assertEqual(self.app.runs.submitted, [])
        self.assertEqual(self.sender.sent_texts(), ["群聊暂未开放，请私聊使用。"])

    def test_group_message_without_mention_is_silent(self):
        self.app.config.data["onebot"].update({"allow_groups": True, "allowed_group_ids": ["777"]})
        self._bind()
        event = self._event("大家好啊")
        event["message_type"] = "group"
        event["group_id"] = 777
        self.adapter.handle_event(event)
        self._wait_for_adapter()
        self.assertEqual(self.app.runs.submitted, [])
        # 群里没 @ 的消息不该刷屏回告。
        self.assertEqual(self.sender.sent_texts(), [])
        record = self.store.lookup_message("10000", "m1")
        self.assertEqual(record["status"], channel_store.MESSAGE_REJECTED)
        self.assertEqual(record["detail"], "group_not_triggered")

    def test_group_mention_reaches_model(self):
        self.app.config.data["onebot"].update({"allow_groups": True, "allowed_group_ids": ["777"]})
        self.store.bind("10000", "group", "20000", self.conversation_id, "777")
        event = self._event("在吗")
        event["message_type"] = "group"
        event["group_id"] = 777
        event["message"] = [{"type": "at", "data": {"qq": "10000"}}] + event["message"]
        self.adapter.handle_event(event)
        self._wait_for_adapter()
        self.assertEqual(len(self.app.runs.submitted), 1)
        self.assertEqual(self.app.runs.submitted[0]["message"], "在吗")
        self.assertEqual(self.sender.sent_texts(), ["这是完整回复。"])
        self.assertEqual(self.store.lookup_message("10000", "m1")["status"],
                         channel_store.MESSAGE_REPLIED)

    def test_group_from_unauthorized_user_is_refused(self):
        self.app.config.data["onebot"].update({"allow_groups": True, "allowed_group_ids": ["777"]})
        event = self._event("在吗", user_id="30000")
        event["message_type"] = "group"
        event["group_id"] = 777
        event["message"] = [{"type": "at", "data": {"qq": "10000"}}] + event["message"]
        self.adapter.handle_event(event)
        self._wait_for_adapter()
        self.assertEqual(self.app.runs.submitted, [])
        self.assertIn("不是被允许", self.sender.sent_texts()[0])

    def test_group_confirmation_is_never_accepted(self):
        self._bind()
        self.store.register_confirmation(
            "10000", "cf-g", run_id="run-1", conversation_id=self.conversation_id,
            qq_user_id="20000", qq_group_id="777", tool="pwsh", ttl_seconds=600,
        )
        self.app.runs.runs["run-1"] = {
            "id": "run-1", "conversation_id": self.conversation_id, "status": "waiting",
        }
        self.app.runs.snapshot = [{"confirm_id": "cf-g", "status": "pending"}]
        group_message = ParsedMessage(
            account_id="10000", message_id="g1", scope="group",
            qq_user_id="20000", qq_group_id="777", text="确认 cf-g",
        )
        self.adapter._confirm_command(group_message, {"allowed_user_ids": ["20000"]})
        self.assertEqual(self.app.runs.confirm_calls, [])
        self.assertIn("只能由收到通知的私聊用户处理", self.sender.sent_texts()[0])

    def test_unbound_user_gets_hint_not_auto_binding(self):
        event = self._event()
        self.adapter.handle_event(event)
        self._wait_for_adapter()
        self.assertEqual(self.app.runs.submitted, [])
        self.assertEqual(self.store.list_bindings(), [])
        self.assertIn("尚未绑定", self.sender.sent_texts()[0])

    def test_duplicate_event_does_not_submit_twice(self):
        self._bind()
        event = self._event()
        self.adapter.handle_event(event)
        self.adapter.handle_event(event)
        self._wait_for_adapter()
        self.assertEqual(len(self.app.runs.submitted), 1)
        self.assertEqual(self.sender.sent_texts(), ["这是完整回复。"])
        self.assertEqual(self.store.lookup_message("10000", "m1")["status"],
                         channel_store.MESSAGE_REPLIED)

    def test_private_message_submits_run_and_delivers_full_reply(self):
        self._bind()
        self._process(private_message("你好"))
        self.assertEqual(len(self.app.runs.submitted), 1)
        submitted = self.app.runs.submitted[0]
        self.assertEqual(submitted["conversation_id"], self.conversation_id)
        self.assertEqual(submitted["message"], "你好")
        self.assertEqual(self.sender.sent_texts(), ["这是完整回复。"])
        self.assertEqual(
            self.store.lookup_message("10000", "1")["status"], channel_store.MESSAGE_REPLIED
        )

    def test_long_reply_is_segmented(self):
        self._bind()
        self.app.runs.reply_text = "段" * 8000
        self._process(private_message())
        texts = self.sender.sent_texts()
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(text) <= 3000 for text in texts))
        self.assertEqual("".join(texts), "段" * 8000)

    def test_active_run_conflict_is_reported(self):
        self._bind()
        from naiba.core.exceptions import ActiveRunError

        self.app.runs.submit_error = ActiveRunError("run-active")
        self._process(private_message())
        self.assertEqual(self.sender.sent_texts(), ["这个会话上一条回复还在进行中，请等它结束再发。"])
        record = self.store.lookup_message("10000", "1")
        self.assertEqual(record["status"], channel_store.MESSAGE_REJECTED)
        self.assertIn("active_run:run-active", record["detail"])

    def test_missing_model_error_reaches_user(self):
        self._bind()
        self.app.runs.submit_error = ValueError("请先在会话底部选择模型")
        self._process(private_message())
        self.assertIn("无法开始回答", self.sender.sent_texts()[0])

    def test_send_failure_marks_delivery_not_run(self):
        self._bind()
        self.sender.fail_actions.add("send_private_msg")
        self._process(private_message())
        record = self.store.lookup_message("10000", "1")
        # Run 成功（假模型已落 assistant 消息），失败的只是 QQ 投递。
        self.assertEqual(self.app.runs.get("run-1")["status"], "completed")
        self.assertNotEqual(record["status"], channel_store.MESSAGE_REPLIED)
        deliveries = self.store.deliveries_for("10000", "1")
        self.assertEqual(deliveries[0]["status"], channel_store.DELIVERY_FAILED)
        self.assertGreaterEqual(deliveries[0]["attempts"], 1)

    def test_failed_delivery_can_be_retried_without_resending_sent_parts(self):
        self._bind()
        self.app.runs.reply_text = "段" * 5000
        self._process(private_message())
        self.assertEqual(len(self.sender.sent_texts()), 2)
        self.assertTrue(self.adapter.deliver_pending("10000", "1"))
        # 已送达的段不再重发。
        self.assertEqual(len(self.sender.sent_texts()), 2)

    def test_unsupported_segment_is_reported_and_text_still_processed(self):
        self._bind()
        self._process(ParsedMessage(
            account_id="10000", message_id="2", scope="private", qq_user_id="20000",
            qq_group_id="", text="看这个", unsupported=["forward"],
        ))
        self.assertIn("forward", self.sender.sent_texts()[0])
        self.assertEqual(len(self.app.runs.submitted), 1)

    def test_local_path_in_media_is_refused(self):
        self._bind()
        message = ParsedMessage(
            account_id="10000", message_id="3", scope="private", qq_user_id="20000",
            qq_group_id="", text="看图",
            media=[{"kind": "image", "segment": "image", "file": "", "file_id": "",
                    "url": "", "name": "a.png"}],
        )
        attachments, error = self.adapter._collect_attachments(message)
        self.assertEqual(attachments, [])
        self.assertIn("附件没能接收", error)

    def test_media_download_lands_in_managed_uploads(self):
        self._bind()

        class _Response:
            def __init__(self, data):
                self._data = data

            def read(self, size=-1):
                data, self._data = self._data, b""
                return data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        from unittest import mock

        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        message = ParsedMessage(
            account_id="10000", message_id="4", scope="private", qq_user_id="20000",
            qq_group_id="", text="看图",
            media=[{"kind": "image", "segment": "image", "file": "", "file_id": "",
                    "url": "http://127.0.0.1:1/a.png", "name": "a.png"}],
        )
        with mock.patch("naiba.net.open", return_value=_Response(png)):
            attachments, error = self.adapter._collect_attachments(message)
        self.assertEqual(error, "")
        self.assertEqual(len(attachments), 1)
        path = Path(attachments[0]["path"])
        self.assertTrue(path.is_file())
        self.assertIn("uploads", str(path))
        self.assertTrue(path.exists())

    def test_confirmation_is_posted_to_bound_user_only(self):
        self._bind()
        self.app.runs.snapshot = [{
            "confirm_id": "cf-9", "run_id": "run-1", "conversation_id": self.conversation_id,
            "tool": "write_file", "reason": "需要确认", "status": "pending",
            "parameters": {"path": "a.txt"},
        }]
        self.app.runs.runs["run-1"] = {
            "id": "run-1", "conversation_id": self.conversation_id, "status": "waiting",
        }
        self.adapter._sync_confirmations(self.app.runs.runs["run-1"], set())
        texts = self.sender.sent_texts()
        self.assertTrue(any("cf-9" in text for text in texts))
        record = self.store.get_confirmation("10000", "cf-9")
        self.assertEqual(record["qq_user_id"], "20000")

    def test_confirm_command_from_other_user_is_refused(self):
        self._bind()
        self.store.register_confirmation(
            "10000", "cf-10", run_id="run-1", conversation_id=self.conversation_id,
            qq_user_id="20000", qq_group_id="", tool="pwsh", ttl_seconds=600,
        )
        self.app.runs.runs["run-1"] = {
            "id": "run-1", "conversation_id": self.conversation_id, "status": "running",
        }
        other = private_message("确认 cf-10", user_id="30000")
        self.adapter._confirm_command(other, {"allowed_user_ids": ["20000"]})
        self.assertEqual(self.app.runs.confirm_calls, [])
        self.assertIn("只能由收到通知的私聊用户处理", self.sender.sent_texts()[0])

    def test_expired_confirmation_cannot_be_approved(self):
        self._bind()
        self.store.register_confirmation(
            "10000", "cf-11", run_id="run-1", conversation_id=self.conversation_id,
            qq_user_id="20000", qq_group_id="", tool="pwsh", ttl_seconds=1,
        )
        self.app.runs.runs["run-1"] = {
            "id": "run-1", "conversation_id": self.conversation_id, "status": "running",
        }
        _force_expire(self.store, "cf-11")
        self.adapter._confirm_command(private_message("确认 cf-11"), {"allowed_user_ids": ["20000"]})
        self.assertEqual(self.app.runs.confirm_calls, [])
        self.assertIn("已失效", self.sender.sent_texts()[0])

    def test_valid_confirmation_is_forwarded(self):
        self._bind()
        self.store.register_confirmation(
            "10000", "cf-12", run_id="run-1", conversation_id=self.conversation_id,
            qq_user_id="20000", qq_group_id="", tool="pwsh", ttl_seconds=600,
        )
        self.app.runs.runs["run-1"] = {
            "id": "run-1", "conversation_id": self.conversation_id, "status": "waiting",
        }
        self.app.runs.snapshot = [{"confirm_id": "cf-12", "status": "pending"}]
        self.adapter._confirm_command(private_message("确认 cf-12"), {"allowed_user_ids": ["20000"]})
        self.assertEqual(self.app.runs.confirm_calls, [("run-1", "cf-12", None)])

    def test_reject_command_is_forwarded(self):
        self._bind()
        self.store.register_confirmation(
            "10000", "cf-13", run_id="run-1", conversation_id=self.conversation_id,
            qq_user_id="20000", qq_group_id="", tool="pwsh", ttl_seconds=600,
        )
        self.app.runs.runs["run-1"] = {
            "id": "run-1", "conversation_id": self.conversation_id, "status": "waiting",
        }
        self.app.runs.snapshot = [{"confirm_id": "cf-13", "status": "pending"}]
        self.adapter._confirm_command(private_message("拒绝 cf-13"), {"allowed_user_ids": ["20000"]})
        self.assertEqual(self.app.runs.reject_calls, [("run-1", "cf-13")])

    def test_run_cancel_is_reported_to_user(self):
        self._bind()
        self.app.runs.submit_error = None
        original = self.app.runs.submit_chat

        def submit_cancelled(body):
            run = original(body)
            self.app.runs.runs[run["id"]]["status"] = "cancelled"
            return run

        self.app.runs.submit_chat = submit_cancelled
        self._process(private_message())
        self.assertIn("已被取消", self.sender.sent_texts()[-1])

    def test_recovery_redelivers_after_restart(self):
        self._bind()
        # 模拟"Run 已提交、回复已落库，但进程在投递前退出"。
        self.store.record_message(
            "10000", "m-restart", scope="private", qq_user_id="20000",
            conversation_id=self.conversation_id, run_id="run-x",
            status=channel_store.MESSAGE_SUBMITTED,
        )
        self.app.runs.runs["run-x"] = {
            "id": "run-x", "conversation_id": self.conversation_id, "status": "completed",
        }
        self.app.storage.add_message(
            self.conversation_id, "assistant", "重启前生成的回复", {"run_id": "run-x"}
        )
        result = self.adapter.recover_pending()
        self.assertGreaterEqual(result["recovered"], 1)
        self._wait_for_adapter()
        self.assertIn("重启前生成的回复", self.sender.sent_texts())

    def test_interrupted_before_submit_is_marked_failed(self):
        """崩溃停在「已记录、未提交」：必须留痕，不能永远显示进行中。"""
        self.store.record_message("10000", "m-ghost", scope="private", qq_user_id="20000")
        # 宽限期内的记录（设置页保存触发的重启窗口）不能被误判。
        self.assertEqual(self.adapter.recover_incomplete(older_than_ms=600_000), 0)
        self.assertEqual(
            self.store.lookup_message("10000", "m-ghost")["status"],
            channel_store.MESSAGE_RECEIVED,
        )
        self.assertEqual(self.adapter.recover_incomplete(older_than_ms=0), 1)
        record = self.store.lookup_message("10000", "m-ghost")
        self.assertEqual(record["status"], channel_store.MESSAGE_FAILED)
        self.assertIn("interrupted_before_submit", record["detail"])

    def test_mistyped_media_is_refused(self):
        """声称是图片、内容却不是图片：不落库，回告用户。"""
        self._bind()

        class _Response:
            def __init__(self, data):
                self._data = data

            def read(self, size=-1):
                data, self._data = self._data, b""
                return data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        from unittest import mock

        message = ParsedMessage(
            account_id="10000", message_id="5", scope="private", qq_user_id="20000",
            qq_group_id="", text="看图",
            media=[{"kind": "image", "segment": "image", "file": "", "file_id": "",
                    "url": "http://127.0.0.1:1/a.png", "name": "a.png"}],
        )
        with mock.patch("naiba.net.open", return_value=_Response(b"MZ\x90\x00not-an-image")):
            attachments, error = self.adapter._collect_attachments(message)
        self.assertEqual(attachments, [])
        self.assertIn("内容不像图片文件", error)

    def test_oversize_media_is_refused(self):
        self._bind()
        from naiba.channels.adapter import MAX_MEDIA_BYTES

        class _Response:
            def read(self, size=-1):
                return b"A" * (MAX_MEDIA_BYTES + 1)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        from unittest import mock

        message = ParsedMessage(
            account_id="10000", message_id="6", scope="private", qq_user_id="20000",
            qq_group_id="", text="看文件",
            media=[{"kind": "file", "segment": "file", "file": "",
                    "file_id": "", "url": "http://127.0.0.1:1/big.bin", "name": "big.bin"}],
        )
        with mock.patch("naiba.net.open", return_value=_Response()):
            attachments, error = self.adapter._collect_attachments(message)
        self.assertEqual(attachments, [])
        self.assertIn("上限", error)


class WebSocketAuthTests(unittest.TestCase):
    """反向 WebSocket 的路径与口令校验（本地假 NapCat，不访问真实 QQ）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _service(self, **overrides):
        from naiba.channels.service import OneBotChannelService

        app = _FakeApp(self.root)
        cfg = {
            "enabled": True, "listen_host": "127.0.0.1", "listen_port": 0,
            "path": "/onebot/v11/ws", "access_token": "onebot-secret",
            "allowed_user_ids": ["20000"], "allow_groups": False,
        }
        cfg.update(overrides)
        app.config.data["onebot"] = cfg
        service = OneBotChannelService(app)
        return service, app

    def _port(self, service):
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", service._server.socket.getsockname()[1]))
            return sock.getsockname()[1]

    def test_start_disabled_when_not_enabled(self):
        service, _ = self._service(enabled=False)
        status = service.start()
        self.assertEqual(status["state"], "disabled")
        self.assertFalse(status["listening"])

    def test_start_refuses_without_token(self):
        service, _ = self._service(access_token="")
        status = service.start()
        self.assertEqual(status["state"], "error")
        self.assertIn("口令", status["error"])

    def test_start_refuses_without_allow_list(self):
        service, _ = self._service(allowed_user_ids=[])
        status = service.start()
        self.assertEqual(status["state"], "error")
        self.assertIn("允许的 QQ 号", status["error"])

    def test_authorization_and_path_are_enforced(self):
        from websockets.sync.client import connect

        service, app = self._service()
        # 端口 0 由系统分配：从 server 上读出真实端口。
        service.start()
        try:
            self.assertEqual(service.status()["state"], "listening")
            port = service._server.socket.getsockname()[1]
            with self.assertRaises(Exception):
                connect(f"ws://127.0.0.1:{port}/wrong/path", additional_headers={
                    "Authorization": "Bearer onebot-secret"}, open_timeout=5)
            with self.assertRaises(Exception):
                connect(f"ws://127.0.0.1:{port}/onebot/v11/ws", additional_headers={
                    "Authorization": "Bearer wrong"}, open_timeout=5)
            with connect(f"ws://127.0.0.1:{port}/onebot/v11/ws", additional_headers={
                    "Authorization": "Bearer onebot-secret"}, open_timeout=5) as ws:
                # 连上即代表鉴权通过；发一个 message 事件，假 NapCat 会收到回告。
                ws.send(json.dumps({
                    "post_type": "message", "message_type": "private",
                    "self_id": 10000, "message_id": 1, "user_id": 20000,
                    "message": [{"type": "text", "data": {"text": "你好"}}],
                }))
                deadline = time.time() + 5
                while time.time() < deadline and not service.adapter.store.lookup_message("10000", "1"):
                    time.sleep(0.05)
                self.assertIsNotNone(service.adapter.store.lookup_message("10000", "1"))
        finally:
            service.stop(timeout=3)
        self.assertEqual(service.status()["state"], "disabled")

    def test_global_access_token_does_not_authorize_onebot(self):
        from websockets.sync.client import connect

        service, _ = self._service()
        service.start()
        try:
            port = service._server.socket.getsockname()[1]
            with self.assertRaises(Exception):
                connect(f"ws://127.0.0.1:{port}/onebot/v11/ws", additional_headers={
                    "Authorization": "Bearer global-token"}, open_timeout=5)
        finally:
            service.stop(timeout=3)

    def test_stop_closes_connection_and_adapter_threads(self):
        service, _ = self._service()
        service.start()
        service.stop(timeout=3)
        status = service.status()
        self.assertFalse(status["listening"])
        self.assertFalse(status["connected"])

    def test_restart_rearms_adapter_for_delivery(self):
        """设置保存会触发 restart：重启后消息必须还能投递（曾因 _stop 未清而永久静默）。"""
        service, _ = self._service()
        service.start()
        service.stop(timeout=3)
        self.assertTrue(service.adapter._stop.is_set())
        service.start()
        try:
            self.assertFalse(
                service.adapter._stop.is_set(), "重启后适配器仍处于停机状态，回复永远不会投递"
            )
        finally:
            service.stop(timeout=3)


class PackagingSelfCheckTests(unittest.TestCase):
    def test_spec_and_self_check_include_websockets(self):
        spec = (ROOT / "naiba-chat.spec").read_text(encoding="utf-8")
        self.assertIn("websockets.sync.server", spec)
        launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn("websockets.sync.server", launcher)
        self.assertIn("naiba.channels.service", launcher)

    def test_release_workflow_pins_websockets(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("websockets==15.0.1", workflow)


if __name__ == "__main__":
    unittest.main()
