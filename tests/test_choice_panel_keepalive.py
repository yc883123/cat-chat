# -*- coding: utf-8 -*-
"""护栏：AI 选项面板「该弹没弹」的两条修复口径（识别词表 + 面板保活）。

背景（2026-09-16）：
- 识别层旧 CUE 词表只认「请选择 / 选哪个 / pick one…」，AI 写「以下是当前的选项：」这类
  高频措辞时整组漏判；
- 前端 `pendingChoiceMessage()` 从末尾向前扫描时，遇到「无选项的 assistant」立即 return null，
  于是选项消息之后只要再追加一条 assistant 消息（followup 轮次 / 后台任务回执 / 错误重试），
  未回答的面板就被整块撤掉，且刷新也回不来；
- 后端会话详情接口原先只给最后一条 assistant 消息补齐 choices，保活后的较早消息没有数据源。

不变量：
- 点过「完成」的消息不再弹（showChoiceButtons 的 known.done 短路）；
- 用户已回复过的旧选项不得重新弹出（前端遇 user 停止、后端遇 user 停止）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.core.choices import backfill_turn_choice_groups, detect_choice_groups  # noqa: E402

MESSAGES_JS = (ROOT / "public/js/04-messages.js").read_text(encoding="utf-8")
HTTP_PY = (ROOT / "naiba/http.py").read_text(encoding="utf-8")
CHAT_JS = (ROOT / "public/js/12-chat-input.js").read_text(encoding="utf-8")
CSS = (ROOT / "public/styles.css").read_text(encoding="utf-8")


class PendingChoiceMessageKeepaliveTests(unittest.TestCase):
    """前端：跳过无选项 assistant 继续向前找，遇 user 才停止。"""

    def test_scans_past_assistant_without_choices(self):
        self.assertIn("if (message?.role !== 'assistant') continue;", MESSAGES_JS)
        self.assertNotIn("if (message?.role === 'assistant') return null;", MESSAGES_JS)

    def test_still_stops_at_user_message(self):
        self.assertIn("if (message?.role === 'user') return null;", MESSAGES_JS)


class HistoryBackfillTests(unittest.TestCase):
    """后端：给「最后一条 user 之后」的全部 assistant 消息补齐选项数据（幂等）。"""

    def test_http_uses_shared_backfill(self):
        self.assertIn('backfill_turn_choice_groups(conversation["messages"])', HTTP_PY)

    def test_backfill_fills_every_assistant_after_last_user(self):
        messages = [
            {"role": "assistant", "metadata": {}, "content": "上一轮：请选择：\n1. 旧甲\n2. 旧乙"},
            {"role": "user", "content": "旧甲"},
            {"role": "assistant", "metadata": {}, "content": "好的！以下是当前的选项：\n1. 甲\n2. 乙"},
            {"role": "assistant", "metadata": {}, "content": "补充说明，本段没有选项。"},
        ]
        backfill_turn_choice_groups(messages)
        self.assertEqual(messages[2]["metadata"]["choices"], ["甲", "乙"])
        self.assertEqual(messages[2]["metadata"]["choice_groups"][0]["prompt"], "好的！以下是当前的选项：")
        self.assertEqual(messages[3]["metadata"]["choices"], [])
        # 最后一条 user 之前的历史保持原样，不回溯改写
        self.assertEqual(messages[0]["metadata"], {})

    def test_backfill_keeps_stored_metadata(self):
        stored = [{"prompt": "视觉", "choices": ["写实", "动漫"], "mode": "single", "source": "explicit"}]
        messages = [{
            "role": "assistant",
            "metadata": {"choice_groups": stored, "choices": ["写实", "动漫"]},
            "content": "正文里其实没有可识别的选项文本。",
        }]
        backfill_turn_choice_groups(messages)
        self.assertEqual(messages[0]["metadata"]["choice_groups"], stored)

    def test_backfill_stops_at_user(self):
        messages = [
            {"role": "assistant", "metadata": {}, "content": "请选择：\n1. A\n2. B"},
            {"role": "user", "content": "A"},
        ]
        backfill_turn_choice_groups(messages)
        self.assertEqual(messages[0]["metadata"], {})

    def test_backfill_tolerates_broken_metadata(self):
        messages = [{"role": "assistant", "metadata": None, "content": "选项如下：\n1. A\n2. B"}]
        backfill_turn_choice_groups(messages)
        self.assertEqual(messages[0]["metadata"]["choices"], ["A", "B"])


class CustomReplyWiringTests(unittest.TestCase):
    """「自定义回复」：选项面板里与预设选项**一律并存**的一项（就地输入框）。

    行为级校验见归档的 verify/choice_custom_reply_check.mjs（抽取同一批函数真执行；该脚本 2026-09-16 归档）；
    这里只守源码接口，防止后续重构把并存口径改回互斥。
    """

    def test_custom_state_and_helpers_present(self):
        for fragment in (
            "entry.customs",
            "function choiceCustom(entry, index)",
            "function choiceHasAnswer(entry, index)",
            "function choiceAnswerItems(entry, index)",
        ):
            self.assertIn(fragment, CHAT_JS, fragment)

    def test_custom_coexists_with_options(self):
        # 一律并存：自定义作为额外一项追加，单选也不互斥
        self.assertIn("if (custom) items.push(custom);", CHAT_JS)

    def test_custom_counts_as_answer_for_validation(self):
        # 「完成」校验与主按钮禁用都按「答了（选项/自定义）或显式跳过」判定
        self.assertIn("choiceResolved(entry, entry.index)", CHAT_JS)
        self.assertIn("!choiceResolved(entry, entry.index)", CHAT_JS)

    def test_custom_reply_ui_and_styles(self):
        for fragment in ("choice-custom-toggle", "choice-custom-input", "choice-custom-clear", "pendingChoiceCustomFocus"):
            self.assertIn(fragment, CHAT_JS, fragment)
        for fragment in (".choice-custom-input", ".choice-custom-toggle"):
            self.assertIn(fragment, CSS, fragment)

    def test_selected_summary_block_removed(self):
        # 「已选：…」摘要与选项按钮高亮重复（用户反馈"多余"）：渲染与样式一并移除
        self.assertNotIn("choice-selection-summary", CHAT_JS)
        self.assertNotIn(".choice-summary-item", CSS)

    def test_single_choice_can_be_unpicked(self):
        # 再点一次已选中的单选选项 = 取消（修复"选中了，再选一次不能取消"）
        self.assertIn("if (picked.length === 1 && picked[0] === choice) {", CHAT_JS)
        self.assertIn("entry.answers[entry.index] = [];", CHAT_JS)

    def test_complete_sends_directly_without_composer(self):
        # 2026-09-16：点「完成」= 直接发送，不再把答案填进输入框（长答案平铺太占地方）
        self.assertIn("Promise.resolve(sendMessage(text)).finally", CHAT_JS)
        self.assertIn("go.textContent = last ? '完成' : '下一题';", CHAT_JS)
        self.assertNotIn("fillComposerAnswer", CHAT_JS)
        self.assertNotIn("选择已填入输入框", CHAT_JS)

    def test_answer_block_avoids_double_colon(self):
        # 题目自带冒号结尾（"请选择下一步："）时答案块不出现「：：」
        self.assertIn("const separator = /[：:]$/.test(group.prompt) ? '' : '：';", CHAT_JS)

    def test_custom_survives_reevent_sync(self):
        # 重复事件同步时按题号对齐，不越界、不串题
        self.assertIn("known.customs = groups.map", CHAT_JS)
        self.assertIn("known.customOpen = groups.map", CHAT_JS)


class ChoiceSkipTests(unittest.TestCase):
    """每题可跳过；全部跳过也能发送（发送内容为明确的跳过说明）。"""

    def test_skip_helpers_present(self):
        for fragment in (
            "function choiceSkipped(entry, index)",
            "function choiceResolved(entry, index)",
            "function toggleChoiceSkip(entry, index)",
        ):
            self.assertIn(fragment, CHAT_JS, fragment)

    def test_skip_counts_as_resolved(self):
        self.assertIn("return choiceHasAnswer(entry, index) || choiceSkipped(entry, index);", CHAT_JS)
        # 「下一题 / 完成」的门槛改为「答了或跳过了」
        self.assertIn("!choiceResolved(entry, index)", CHAT_JS)
        self.assertIn("!choiceResolved(entry, entry.index)", CHAT_JS)

    def test_all_skipped_sends_placeholder(self):
        self.assertIn("const text = block || '（已跳过全部选择题）';", CHAT_JS)

    def test_skip_button_and_badge(self):
        self.assertIn("skip.dataset.choiceNav = 'skip';", CHAT_JS)
        self.assertIn("'取消跳过'", CHAT_JS)
        self.assertIn(".choice-mode-badge.is-skip", CSS)
        self.assertIn("已跳过本题", CHAT_JS)


class ChoicePreviewCollapseTests(unittest.TestCase):
    """正文「选择题」静态块：折叠成一行（点开看选项），并标记已答/未答。"""

    PREVIEW_JS = (ROOT / "public/js/17-choice-groups.js").read_text(encoding="utf-8")
    EVENTS_JS = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def test_preview_renders_collapsed_toggle(self):
        for fragment in (
            'class="choice-preview is-collapsed"',
            "data-choice-preview-toggle",
            'aria-expanded="false"',
            'class="choice-preview-body"',
            "choice-preview-subject",
            "choice-preview-meta",
        ):
            self.assertIn(fragment, self.PREVIEW_JS, fragment)

    def test_toggle_wired_in_events(self):
        self.assertIn("data-choice-preview-toggle", self.EVENTS_JS)
        self.assertIn("is-collapsed", self.EVENTS_JS)

    def test_answered_state_marking(self):
        self.assertIn("markChoicePreviewAnsweredState", MESSAGES_JS)
        self.assertIn("is-answered", CSS)

    def test_collapse_styles(self):
        self.assertIn(".choice-preview.is-collapsed .choice-preview-body", CSS)
        self.assertIn(".choice-preview.is-answered .choice-preview-meta::after", CSS)

    def test_old_always_open_bar_removed(self):
        # 旧的「固定展开」条已退役：不再存在无折叠态的静态块
        self.assertNotIn("choice-preview-bar", self.PREVIEW_JS)
        self.assertNotIn(".choice-preview-bar", CSS)


class CueRelaxationWiringTests(unittest.TestCase):
    """识别层：新措辞必须已在 CUE_RE 中，且不引入裸 select 触发。"""

    def test_new_cues_present_in_source(self):
        source = (ROOT / "naiba/core/choices.py").read_text(encoding="utf-8")
        for fragment in ("以下是", "(?:如下", "你可以", "请从", "here (?:are|is)"):
            self.assertIn(fragment, source, fragment)

    def test_bare_select_still_rejected(self):
        # 旧口径保留：SQL 的裸 SELECT 不得被当成选项意图
        self.assertEqual(detect_choice_groups("SELECT * FROM users\n1. a\n2. b"), [])


if __name__ == "__main__":
    unittest.main()
