# -*- coding: utf-8 -*-
"""护栏：自动压缩上下文（docs/plans/2026-10-26-自动压缩上下文计划.md）。

方案：**阈值触发、同一模型隐藏总结、摘要定稿后稳定复用**。
- 不引入摘要模型：总结请求用当前对话本轮实际选中的同一个 profile（不换模型）；
- 隐藏请求：不挂工具、不流式，只喂「将被压缩的历史 + 旧摘要」，要求结构化交接输出；
- 保留最近 N 个**完整轮次**原文（不按消息行数硬切）；
- 格式与长度校验通过才写标记；失败/不合格就不整理，本轮按原历史继续；
- **旧摘要不丢**：再次整理时交给模型的是「旧摘要 + 新进入区间的历史」。
覆盖：
1. 设置解析/校验；2. 触发判断与按轮次切分；3. 总结请求构造与校验；
4. 标记写入与重放（含旧摘要合并）；5. 撤销；6. 接线；7. chat 钩子。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core import compaction  # noqa: E402
from naiba.core.history import (  # noqa: E402
    COMPACTION_REPLAY_PREAMBLE,
    build_model_history,
    compaction_replay_message,
)
from naiba.core.messages import MESSAGE_METADATA_KEYS, MetadataKeys  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


def _row(role, content, metadata=None, row_id="m1"):
    return {"id": row_id, "role": role, "content": content, "metadata": metadata or {}}


def _marker(summary, **extra):
    return {MetadataKeys.COMPACTION: {"at": 1, "source": "auto", "summary": summary, **extra}}


class CompactionSettingsTests(unittest.TestCase):
    def test_defaults_are_auto_at_70_percent(self):
        settings = compaction.resolve_settings(None)
        self.assertEqual(settings["mode"], "auto")
        # 70 而不是 80：整理要额外发一次总结请求、之后还要正常回复，触发点必须提前。
        self.assertEqual(settings["threshold_percent"], 70)
        self.assertEqual(settings["retain_turns"], 3)
        self.assertEqual(settings["summary_max_tokens"], 2048)

    def test_partial_dict_fills_missing_keys(self):
        settings = compaction.resolve_settings({"mode": "off"})
        self.assertEqual(settings["mode"], "off")
        self.assertEqual(settings["retain_turns"], compaction.COMPACT_DEFAULTS["retain_turns"])

    def test_legacy_keys_are_tolerated(self):
        """旧版（本地抽取）写过的 retain_messages / digest_max_chars 不该让配置报错。"""
        settings = compaction.resolve_settings({"mode": "auto", "retain_messages": 8,
                                                "digest_max_chars": 12000})
        self.assertEqual(settings["mode"], "auto")
        self.assertNotIn("retain_messages", compaction.SETTING_KEYS)

    def test_invalid_values_fall_back(self):
        settings = compaction.resolve_settings({
            "mode": "bogus", "threshold_percent": "abc",
            "retain_turns": 0, "summary_max_tokens": 5,
        })
        self.assertEqual(settings["mode"], "auto")
        self.assertEqual(settings["threshold_percent"], 70)
        self.assertEqual(settings["retain_turns"], 1, "下限收紧到 1 轮")
        self.assertEqual(settings["summary_max_tokens"], 256, "下限收紧到 256")

    def test_conversation_override_wins(self):
        settings = compaction.resolve_settings({"mode": "auto"})
        self.assertEqual(compaction.effective_mode(settings, {"compaction_mode": "off"}), "off")
        self.assertEqual(compaction.effective_mode(settings, {"compaction_mode": ""}), "auto")
        self.assertEqual(compaction.effective_mode(settings, None), "auto")

    def test_threshold_percent(self):
        settings = compaction.resolve_settings({"threshold_percent": 70, "summary_max_tokens": 256})
        limit = 100000
        self.assertFalse(compaction.should_compact(settings, used=69999, limit=limit))
        self.assertTrue(compaction.should_compact(settings, used=70000, limit=limit))

    def test_unknown_window_never_triggers(self):
        settings = compaction.resolve_settings(None)
        self.assertFalse(compaction.should_compact(settings, used=99999, limit=0))

    def test_reserve_lowers_the_trigger_point(self):
        """预留越大触发越早：给总结请求和正常回复留空间。

        ceiling = limit - (summary_max_tokens + reserve + SUMMARY_OUTPUT_SLACK)；
        reserve=0 时 2048+1024=3072，窗口 10000 只剩 6928 —— 用量 6000 不触发；
        再预留 3000 后只剩 3928 —— 同样的 6000 必须触发。
        """
        settings = compaction.resolve_settings({"threshold_percent": 95, "summary_max_tokens": 2048})
        limit = 10000
        self.assertFalse(compaction.should_compact(settings, used=6000, limit=limit, reserve=0))
        self.assertTrue(compaction.should_compact(settings, used=6000, limit=limit, reserve=3000))


class CompactionRegionTests(unittest.TestCase):
    def _turns(self, count=6, per_turn=2):
        """每轮 = 一条 user + 若干 assistant。"""
        rows = []
        for turn in range(count):
            rows.append(_row("user", f"第{turn}轮提问", row_id=f"u{turn}"))
            for extra in range(per_turn - 1):
                rows.append(_row("assistant", f"第{turn}轮回复{extra}", row_id=f"a{turn}_{extra}"))
        return rows

    def test_keeps_recent_complete_turns(self):
        rows = self._turns(6)
        boundary = compaction.retain_boundary(rows, 3)
        # 每轮 = user + 1 条 assistant（per_turn=2），所以第 2 轮最后一条是 a2_0。
        self.assertEqual(rows[boundary]["id"], "a2_0")
        retained = rows[boundary + 1:]
        user_rows = [r for r in retained if r["role"] == "user"]
        self.assertEqual(len(user_rows), 3, "保留区必须是 3 个完整轮次")
        self.assertEqual(retained[0]["role"], "user", "保留区必须从一轮的提问开始，不能劈开轮次")

    def test_boundary_negative_when_not_enough_turns(self):
        self.assertEqual(compaction.retain_boundary(self._turns(2), 3), -1)
        self.assertEqual(compaction.retain_boundary([], 3), -1)

    def test_single_turn_retained(self):
        rows = self._turns(3)
        boundary = compaction.retain_boundary(rows, 1)
        self.assertEqual(rows[boundary + 1]["role"], "user")
        self.assertEqual(len([r for r in rows[boundary + 1:] if r["role"] == "user"]), 1)

    def test_estimate_never_encodes_images(self):
        rows = [_row("user", "看图", {MetadataKeys.ATTACHMENTS: [
            {"path": "Z:/definitely/missing/a.png"},
            {"path": "Z:/definitely/missing/b.mp4"},
        ]})]
        tokens = compaction.estimate_rows_tokens(rows)
        self.assertGreaterEqual(tokens, compaction.IMAGE_ESTIMATE_TOKENS,
                                "图片按固定成本计入，且只计图片扩展名")

    def test_estimate_counts_tool_results(self):
        rows = [_row("assistant", "答", {MetadataKeys.TOOL_RUNS: [
            {"tool": "read_file", "success": True, "result": "x" * 400},
        ]})]
        self.assertGreater(compaction.estimate_rows_tokens(rows), 100)


class CompactionSummaryRequestTests(unittest.TestCase):
    """总结请求：只喂历史 + 固定指令，不挂工具，图片降级成文字。"""

    def test_messages_end_with_instruction_and_carry_history(self):
        history = [
            {"role": "user", "content": "把打标器的删除功能做完"},
            {"role": "assistant", "content": "我先看接口"},
            {"role": "tool", "content": "class LabelerStore: ...", "tool_call_id": "t1"},
        ]
        messages = compaction.build_summary_messages(history)
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("交接总结", messages[-1]["content"])
        self.assertEqual([m["role"] for m in messages[:3]], ["user", "assistant", "tool"])
        self.assertEqual(messages[2]["tool_call_id"], "t1", "工具调用结构必须带上")

    def test_instruction_covers_all_required_sections(self):
        instruction = compaction.SUMMARIZE_INSTRUCTION
        for keyword in compaction.REQUIRED_SECTION_KEYWORDS:
            self.assertIn(keyword, instruction, f"指令必须覆盖分区：{keyword}")
        self.assertIn("<compacted-summary>", instruction, "必须要求合并旧摘要")

    def test_images_become_text_markers(self):
        history = [{"role": "user", "content": [
            {"type": "text", "text": "看这两张图"},
            {"type": "image", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]}]
        messages = compaction.build_summary_messages(history)
        content = messages[0]["content"]
        self.assertIn("看这两张图", content)
        self.assertIn("[图片]", content)
        self.assertNotIn("base64", content, "总结请求不该带上图片字节")

    def test_empty_history_still_returns_instruction(self):
        messages = compaction.build_summary_messages([])
        self.assertEqual(len(messages), 1)
        self.assertIn("交接总结", messages[0]["content"])


GOOD_SUMMARY = """## 当前目标
- 把打标器的任务删除功能做完并补测试。

## 用户约束
- UI 文案要用户视角，不要实现视角术语。

## 已确认事实
- 标注库按内容 + 配置指纹全局复用。

## 已作决定
- 删除任务不动 annotations。

## 完成进度
- 后端接口与前端按钮已完成。

## 待办事项
- 补浏览器冒烟。

## 关键工具结果与文件路径
- naiba/labeler/jobs.py::delete_tasks

## 尚不确定的事项
- 是否需要批量选择。

## 下一步
- 跑全量单测。
"""


class CompactionValidationTests(unittest.TestCase):
    def test_good_summary_passes(self):
        ok, reason = compaction.validate_summary(GOOD_SUMMARY)
        self.assertTrue(ok, reason)

    def test_too_short_rejected(self):
        ok, reason = compaction.validate_summary("好的，已完成。")
        self.assertFalse(ok)
        self.assertEqual(reason, "summary_too_short")

    def test_unstructured_prose_rejected(self):
        prose = "这是一段没有分区的散文，" * 20
        ok, reason = compaction.validate_summary(prose)
        self.assertFalse(ok)
        self.assertTrue(
            reason == "summary_not_structured" or reason.startswith("summary_missing_sections"),
            reason,
        )

    def test_missing_sections_rejected(self):
        # 有标题但只覆盖一两个分区：不合格。
        partial = "## 目标\n- 做点事\n\n## 下一步\n- 继续\n" + "补充说明。" * 60
        ok, reason = compaction.validate_summary(partial)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("summary_missing_sections"), reason)

    def test_too_long_rejected(self):
        ok, reason = compaction.validate_summary(GOOD_SUMMARY + "x" * 30000)
        self.assertFalse(ok)
        self.assertEqual(reason, "summary_too_long")

    def test_clean_summary_strips_fences(self):
        wrapped = "```markdown\n" + GOOD_SUMMARY + "\n```"
        cleaned = compaction.clean_summary(wrapped)
        self.assertTrue(cleaned.startswith("## 当前目标"))
        self.assertNotIn("```", cleaned)


class _FakeStorage:
    """只实现 compact_conversation 用到的那一个方法。"""

    def __init__(self):
        self.calls = []
        self.ok = True

    def merge_message_metadata(self, conversation_id, message_id, patch):
        self.calls.append((conversation_id, message_id, patch))
        return self.ok


class _FakeModels:
    def __init__(self, reply=None, raises=None):
        self.reply = reply
        self.raises = raises
        self.calls = []

    def complete(self, profile, messages, options, status=None):
        self.calls.append({"profile": profile, "messages": messages, "options": options})
        if self.raises:
            raise self.raises
        return self.reply


class _FakeApp:
    def __init__(self, reply=None, raises=None):
        self.storage = _FakeStorage()
        self.models = _FakeModels(reply=reply, raises=raises)
        self.config = None


class CompactionCompactConversationTests(unittest.TestCase):
    """触发 → 隐藏总结请求 → 校验 → 写标记。"""

    def setUp(self):
        self.app = _FakeApp(reply=GOOD_SUMMARY)
        self.settings = compaction.resolve_settings({"retain_turns": 1})
        self.profile = {"kind": "online", "model": "probe-default",
                        "model_key": "online:probe-te", "context_window": 8000}

    def _rows(self, turns=6):
        filler = "这是一段较长的历史正文，用来模拟真实长会话里的大段工具输出与讨论。" * 20
        rows = []
        for turn in range(turns):
            rows.append(_row("user", f"第{turn}轮：{filler}", row_id=f"u{turn}"))
            rows.append(_row("assistant", f"第{turn}轮回复：{filler}", row_id=f"a{turn}"))
        return rows

    def _compact(self, **overrides):
        kwargs = dict(
            conversation_id="c1", rows=self._rows(), settings=self.settings,
            profile=self.profile, model_key="online:probe-te", model_name="probe-default",
            limit=8000, used=7000,
        )
        kwargs.update(overrides)
        return compaction.compact_conversation(self.app, **kwargs)

    def test_calls_current_model_once_without_tools(self):
        result = self._compact()
        self.assertTrue(result["compacted"], result)
        self.assertEqual(len(self.app.models.calls), 1, "只发一次总结请求")
        call = self.app.models.calls[0]
        self.assertIs(call["profile"], self.profile, "必须用本轮实际选中的同一 profile")
        self.assertNotIn("tools", call["options"], "总结请求不得挂工具")
        self.assertFalse(call["options"]["stream"], "总结请求不流式")

    def test_marker_records_model_and_generator(self):
        self._compact()
        payload = self.app.storage.calls[0][2][MetadataKeys.COMPACTION]
        self.assertEqual(payload["generator"], "model-summary")
        self.assertEqual(payload["model_name"], "probe-default")
        self.assertEqual(payload["summary"].strip(), GOOD_SUMMARY.strip())
        self.assertGreater(payload["freed_est"], 0)
        self.assertGreater(payload["region_tokens"], 0)

    def test_marker_lands_on_last_region_row(self):
        rows = self._rows()
        self._compact(rows=rows)
        boundary = compaction.retain_boundary(rows, 1)
        self.assertEqual(self.app.storage.calls[0][1], rows[boundary]["id"])

    def test_summary_failure_writes_nothing(self):
        self.app.models.raises = RuntimeError("HTTP 502")
        result = self._compact()
        self.assertFalse(result["compacted"])
        self.assertTrue(result["reason"].startswith("summary_failed"), result)
        self.assertEqual(self.app.storage.calls, [], "失败不得留下任何标记")

    def test_invalid_summary_writes_nothing(self):
        self.app.models.reply = "好的，我已经总结完了。"
        result = self._compact()
        self.assertFalse(result["compacted"])
        self.assertEqual(result["reason"], "summary_too_short")
        self.assertEqual(self.app.storage.calls, [])

    def test_region_too_small_writes_nothing(self):
        result = self._compact(rows=self._rows(turns=1))
        self.assertFalse(result["compacted"])
        self.assertEqual(result["reason"], "region_too_small")
        self.assertEqual(self.app.models.calls, [], "区间太小时连请求都不该发")

    def test_old_summary_is_fed_back_to_the_model(self):
        """再次整理：交给模型的是「旧摘要 + 新进入区间的历史」，不是只总结原始消息。"""
        rows = self._rows()
        # 旧摘要标记落在第 2 条消息上（属于本次要被整理的区间）。
        rows[2] = {**rows[2], "metadata": _marker("旧的交接总结：目标 A，已完成 B")}
        self._compact(rows=rows)
        sent = self.app.models.calls[0]["messages"]
        joined = "\n".join(str(m.get("content") or "") for m in sent)
        self.assertIn("旧的交接总结：目标 A，已完成 B", joined,
                      "旧摘要必须随区间一起交给模型（否则旧信息会丢）")
        self.assertIn("<compacted-summary>", joined)
        self.assertIn("第3轮", joined, "新进入区间的原始消息也要在")

    def test_marker_write_failure_reports(self):
        self.app.storage.ok = False
        result = self._compact()
        self.assertFalse(result["compacted"])
        self.assertEqual(result["reason"], "marker_write_failed")

    def test_storage_exception_is_swallowed(self):
        self.app.storage.merge_message_metadata = mock.Mock(side_effect=OSError("disk"))
        result = self._compact()
        self.assertFalse(result["compacted"])
        self.assertEqual(result["reason"], "marker_write_failed")

    def test_cancel_event_passed_to_request(self):
        token = mock.Mock()
        self._compact(cancel_event=token)
        self.assertIs(self.app.models.calls[0]["options"]["cancel_event"], token)


class CompactionReplayTests(unittest.TestCase):
    def test_metadata_key_registered(self):
        self.assertEqual(MetadataKeys.COMPACTION, "compaction")
        self.assertIn("compaction", MESSAGE_METADATA_KEYS, "新增 metadata 键必须登记契约表")
        from naiba.core.contracts import MetadataKeys as Reexported

        self.assertEqual(Reexported.COMPACTION, "compaction", "契约 re-export 不能漏")

    def test_history_starts_with_digest_message(self):
        history = build_model_history([
            _row("user", "旧问题一", row_id="a"),
            _row("assistant", "旧回答一", row_id="b"),
            _row("assistant", "旧回答二（带标记）", _marker("用户想做的三件事……"), row_id="c"),
            _row("user", "新问题", row_id="d"),
        ])
        self.assertEqual(history[0]["role"], "user")
        self.assertIn(COMPACTION_REPLAY_PREAMBLE, history[0]["content"])
        self.assertIn("<compacted-summary>", history[0]["content"])
        self.assertIn("用户想做的三件事", history[0]["content"])
        self.assertEqual([m["content"] for m in history[1:]], ["新问题"])
        self.assertEqual(len(history), 2, "标记之前的消息全部被摘要替换")

    def test_last_marker_wins(self):
        history = build_model_history([
            _row("user", "第一段", row_id="a"),
            _row("assistant", "第一答", _marker("摘要一"), row_id="b"),
            _row("user", "第二段", row_id="c"),
            _row("assistant", "第二答", _marker("摘要二"), row_id="d"),
            _row("user", "第三段", row_id="e"),
        ])
        self.assertIn("摘要二", history[0]["content"])
        self.assertNotIn("摘要一", history[0]["content"])
        self.assertEqual(history[-1]["content"], "第三段")

    def test_empty_summary_marker_is_ignored(self):
        history = build_model_history([
            _row("user", "问题", row_id="a"),
            _row("assistant", "回答", {MetadataKeys.COMPACTION: {"summary": "   "}}, row_id="b"),
        ])
        self.assertEqual([m["content"] for m in history], ["问题", "回答"],
                         "空摘要不得把历史清空")

    def test_no_marker_means_byte_identical_history(self):
        """没有整理标记时，重放结果必须与「功能不存在」时完全一致（缓存零影响）。"""
        rows = [
            _row("user", "问题一", row_id="a"),
            _row("assistant", "回答一", row_id="b"),
            _row("user", "问题二", row_id="c"),
        ]
        history = build_model_history(rows)
        self.assertEqual(
            [(m["role"], m["content"]) for m in history],
            [("user", "问题一"), ("assistant", "回答一"), ("user", "问题二")],
        )
        for message in history:
            self.assertNotIn(COMPACTION_REPLAY_PREAMBLE, str(message.get("content")))

    def test_replay_message_is_stable(self):
        first = compaction_replay_message("摘要正文")
        second = compaction_replay_message("摘要正文")
        self.assertEqual(first, second, "摘要消息必须逐字节稳定（前缀缓存）")

    def test_compaction_and_session_start_coexist(self):
        # 同一行同时带两种标记时，整理优先（摘要开场），不会又清空一次。
        history = build_model_history([
            _row("user", "老问题", row_id="a"),
            _row("assistant", "老回答", {**_marker("摘要"), MetadataKeys.SESSION_START: {"at": 1}},
                 row_id="b"),
            _row("user", "新问题", row_id="c"),
        ])
        self.assertIn("<compacted-summary>", history[0]["content"])
        self.assertEqual(history[-1]["content"], "新问题")


class CompactionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = ChatStorage(Path(self.tmp.name) / "test.db")
        self.conversation = self.storage.create_conversation("测试会话")

    def test_marker_write_and_clear_keeps_other_metadata(self):
        message = self.storage.add_message(
            self.conversation["id"], "user", "问题",
            {MetadataKeys.ATTACHMENTS: [{"path": "C:/x/a.png"}]},
        )
        ok = self.storage.merge_message_metadata(
            self.conversation["id"], message["id"],
            {MetadataKeys.COMPACTION: {"at": 1, "summary": "摘要"}},
        )
        self.assertTrue(ok)
        detail = self.storage.get_conversation(self.conversation["id"])
        stored = (detail["messages"][0].get("metadata") or {})
        self.assertEqual(stored[MetadataKeys.COMPACTION]["summary"], "摘要")
        self.assertTrue(stored[MetadataKeys.ATTACHMENTS], "其他写入方的键必须保留")

        self.assertTrue(self.storage.clear_compaction_marker(message["id"]))
        detail = self.storage.get_conversation(self.conversation["id"])
        stored = (detail["messages"][0].get("metadata") or {})
        self.assertNotIn(MetadataKeys.COMPACTION, stored)
        self.assertTrue(stored[MetadataKeys.ATTACHMENTS], "撤销整理不得动其他 metadata")

    def test_clear_missing_marker_returns_false(self):
        message = self.storage.add_message(self.conversation["id"], "user", "问题")
        self.assertFalse(self.storage.clear_compaction_marker(message["id"]))
        self.assertFalse(self.storage.clear_compaction_marker("no-such-message"))


class CompactionConfigWiringTests(unittest.TestCase):
    def test_defaults_and_nested_merge(self):
        from naiba.config import ConfigStore, default_config
        from naiba.paths import PathContext

        self.assertIn("auto_compaction", default_config())
        self.assertEqual(default_config()["auto_compaction"]["mode"], "auto")
        self.assertEqual(default_config()["auto_compaction"]["threshold_percent"], 70)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        # 用户只写了 mode：其余键必须由嵌套默认值补齐，否则前端回显成 undefined。
        path.write_text(json.dumps({"auto_compaction": {"mode": "off"}}), encoding="utf-8")
        config = ConfigStore(path, paths=PathContext.local(Path(tmp.name), path))
        self.assertEqual(config.data["auto_compaction"]["mode"], "off")
        self.assertEqual(config.data["auto_compaction"]["threshold_percent"], 70)

    def test_update_settings_validates(self):
        from naiba.config import ConfigStore
        from naiba.paths import PathContext

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        config = ConfigStore(path, paths=PathContext.local(Path(tmp.name), path))
        config.update_settings({"auto_compaction": {"mode": "remind", "threshold_percent": 60}})
        self.assertEqual(config.data["auto_compaction"]["mode"], "remind")
        self.assertEqual(config.data["auto_compaction"]["threshold_percent"], 60)
        # 旧键名（本地抽取时代的配置）必须被容忍，不能报「不支持的字段」。
        config.update_settings({"auto_compaction": {"mode": "auto", "retain_messages": 8}})
        with self.assertRaises(ValueError):
            config.update_settings({"auto_compaction": {"mode": "sometimes"}})
        with self.assertRaises(ValueError):
            config.update_settings({"auto_compaction": {"mode": "auto", "nope": 1}})

    def test_http_routes_wired(self):
        http = (ROOT / "naiba/http.py").read_text(encoding="utf-8")
        self.assertIn('path.endswith("/compact")', http)
        self.assertIn('path.startswith("/api/compaction/")', http)
        self.assertIn("clear_compaction_marker", http)
        # 手动整理必须用该会话自己的模型（不引入摘要模型）。
        compact_section = http.split('path.endswith("/compact")', 1)[1].split("/session_start", 1)[0]
        self.assertIn("resolve_profile_with_override", compact_section)
        # 工具开关按「本次实际可用工具名集合」收窄：用会话固化的 enabled_tool_ids 走
        # 运行路径同一个 resolver，不手拼全局 agent_tools（否则 PDF/视频指引两种口径）。
        self.assertIn("resolve_allowed_tools", compact_section)
        self.assertIn('conversation.get("enabled_tool_ids")', compact_section)

    def test_frontend_wired(self):
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        settings_js = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        messages_js = (ROOT / "public/js/04-messages.js").read_text(encoding="utf-8")
        bind_js = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        chat_js = (ROOT / "public/js/12-chat-input.js").read_text(encoding="utf-8")
        stream_js = (ROOT / "public/js/11-run-stream.js").read_text(encoding="utf-8")
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        self.assertIn('id="autoCompactionMode"', html)
        self.assertIn("自动整理", html)
        self.assertIn("完整轮次", html)
        self.assertIn("retain_turns", settings_js)
        self.assertIn("compactionAfter", messages_js)
        self.assertIn("data-cancel-compaction", messages_js)
        self.assertIn("cancelCompaction", bind_js)
        self.assertIn("handleCompactionEvent", chat_js)
        self.assertIn("compaction: handleCompactionEvent", chat_js)
        self.assertIn("action === 'started'", chat_js)
        self.assertIn("action === 'compacted' || action === 'failed'", chat_js)
        self.assertIn("contextUsageRing", chat_js)
        self.assertIn("compaction-running", chat_js)
        self.assertIn("document.querySelectorAll('.compaction-running')", stream_js)
        self.assertIn("#contextUsageRing.compaction-running", css)


class CompactionHookTests(unittest.TestCase):
    """run/chat 的轮首钩子：条件不满足时零动作；触发时用同一 profile 发一次总结。"""

    def setUp(self):
        from naiba.run.chat import ConversationRunMixin

        self.mixin = ConversationRunMixin()
        self.mixin.app = mock.Mock()
        self.mixin.app.config.data = {"auto_compaction": {"mode": "off"}}
        self.mixin.app.tool_registry.schemas.return_value = []
        filler = "较长的历史正文，用来让区间够大。" * 90
        self.rows = [_row("user" if i % 2 == 0 else "assistant", f"第{i}条：{filler}",
                          row_id=f"m{i}") for i in range(12)]
        # 钩子估算的是组装后的模型视图（history），不是消息行：这里给一份同样大的，
        # 否则「未达阈值」与「达阈值」两组用例分不开。
        self.history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}条：{filler}"}
            for i in range(12)
        ]

    def _call(self, **overrides):
        kwargs = dict(
            run={"conversation_messages": self.rows},
            snapshot={"allowed_tools": []},
            conversation_id="c1",
            conv_kind="main",
            frozen_conversation={},
            history=self.history,
            profile={"context_window": 8000, "kind": "online"},
            effective="新问题",
            model_key="online:probe-te",
            model_name="probe-default",
            event=mock.Mock(),
            cancel_event=mock.Mock(is_set=lambda: False),
        )
        kwargs.update(overrides)
        return self.mixin._maybe_compact_context(**kwargs)

    def test_off_mode_does_nothing(self):
        result = self._call()
        self.assertEqual(result, {})
        self.mixin.app.models.complete.assert_not_called()
        self.mixin.app.storage.merge_message_metadata.assert_not_called()

    def test_aux_conversation_does_nothing(self):
        self.mixin.app.config.data = {"auto_compaction": {"mode": "auto"}}
        result = self._call(conv_kind="aux")
        self.assertEqual(result, {})
        self.mixin.app.models.complete.assert_not_called()

    def test_remind_mode_only_hints(self):
        self.mixin.app.config.data = {"auto_compaction": {"mode": "remind", "threshold_percent": 30}}
        result = self._call()
        self.assertTrue(result.get("hint"))
        self.mixin.app.models.complete.assert_not_called()
        self.mixin.app.storage.merge_message_metadata.assert_not_called()

    def test_below_threshold_does_nothing(self):
        self.mixin.app.config.data = {"auto_compaction": {"mode": "auto", "threshold_percent": 95}}
        # 短历史：远没到窗口（limit=8000，95% = 7600）。
        short = [{"role": "user", "content": "短短一句话"}]
        result = self._call(history=short)
        self.assertEqual(result, {})
        self.mixin.app.models.complete.assert_not_called()

    def test_in_loop_check_accepts_production_keyword_arguments(self):
        from naiba.run.chat import ConversationRunMixin

        mixin = ConversationRunMixin()
        mixin.app = mock.Mock()
        mixin.app.config.data = {"auto_compaction": {"mode": "auto", "threshold_percent": 95}}
        result = mixin._compact_agent_context(
            run={"conversation_messages": []}, snapshot={"allowed_tools": []},
            conversation_id="c1", conv_kind="main", frozen_conversation={},
            messages=[{"role": "system", "content": "test"}, {"role": "user", "content": "short"}],
            trace_start=1, profile={"context_window": 128000, "kind": "online"},
            model_key="online:probe", model_name="probe", pdf_tools=False, video_tools=False,
            limit=128000, budget=100000, used=1000, event=mock.Mock(),
            cancel_event=mock.Mock(is_set=lambda: False),
        )
        self.assertEqual(result, {})
        mixin.app.models.complete.assert_not_called()

    def test_agent_compaction_uses_latest_reported_input_without_accumulating(self):
        self.mixin.app.config.data = {"auto_compaction": {"mode": "auto", "threshold_percent": 70}}
        self.mixin._maybe_compact_context = mock.Mock(return_value={})
        run = {}
        result = self.mixin._compact_agent_context(
            run=run, snapshot={"allowed_tools": []}, conversation_id="c1", conv_kind="main",
            frozen_conversation={}, messages=[{"role": "system"}, {"role": "user", "content": "x"}],
            trace_start=1, profile={"context_window": 8000, "kind": "online"},
            model_key="online:probe", model_name="probe", pdf_tools=False, video_tools=False,
            limit=8000, budget=7000, used=1200, latest_input_tokens=6000,
            event=mock.Mock(), cancel_event=mock.Mock(is_set=lambda: False),
        )
        self.assertEqual(result, {})
        self.mixin._maybe_compact_context.assert_called_once()
        override = self.mixin._maybe_compact_context.call_args.kwargs["usage_override"]
        self.assertEqual(override["used"], 6000)
        self.assertEqual(override["limit"], 8000)
        self.assertEqual(override["budget"], 7000)
        self.assertTrue(run["_compaction_attempted"])

    def test_compaction_failure_event_includes_internal_reason(self):
        self.mixin.app.config.data = {"auto_compaction": {"mode": "auto", "threshold_percent": 30}}
        self.mixin.app.models.complete.return_value = GOOD_SUMMARY
        self.mixin.app.storage.merge_message_metadata.return_value = False
        event = mock.Mock()
        result = self.mixin._compact_agent_context(
            run={"conversation_messages": self.rows}, snapshot={"allowed_tools": []},
            conversation_id="c1", conv_kind="main", frozen_conversation={},
            messages=[{"role": "system"}, *self.history], trace_start=1,
            profile={"context_window": 8000, "kind": "online"},
            model_key="online:probe-te", model_name="probe-default", pdf_tools=True,
            video_tools=True, limit=8000, budget=7000, used=6000,
            event=event, cancel_event=mock.Mock(is_set=lambda: False),
        )
        self.assertEqual(result, {})
        payload = next(call.args[0] for call in event.call_args_list
                       if call.args[0].get("action") == "failed")
        self.assertEqual(payload["reason"], "marker_write_failed")
        self.assertIn("本轮将沿用原上下文继续", payload["message"])

    def test_auto_mode_uses_same_profile_and_reports_rebuild(self):
        self.mixin.app.config.data = {"auto_compaction": {"mode": "auto", "threshold_percent": 30}}
        self.mixin.app.storage.merge_message_metadata.return_value = True
        self.mixin.app.models.complete.return_value = GOOD_SUMMARY
        profile = {"context_window": 8000, "kind": "online", "model": "probe-default"}
        result = self._call(profile=profile)
        self.assertTrue(result.get("rebuilt"), result)
        # 关键：用本轮同一个 profile 对象发请求，不换模型。
        call = self.mixin.app.models.complete.call_args
        self.assertIs(call[0][0], profile, "必须用本轮实际选中的同一个 profile")
        self.assertNotIn("tools", call[0][2], "总结请求不得挂工具")
        self.mixin.app.storage.merge_message_metadata.assert_called_once()


if __name__ == "__main__":
    unittest.main()
