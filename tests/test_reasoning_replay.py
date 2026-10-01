# -*- coding: utf-8 -*-
"""守门：思考回放限长（双闸门）+ 两个可调参数 + 三个调用点透传。

病历（2026-09-19，真实库 `D:\\naibachatdata\\chat.db` 只读探针量化）：
用户最早看到的现象是「MiMo 2.5 会话里 AI 的总结**每次都是同一条文字**」，当时结论是
「模型尽力了但做不到」。量化后确认那不是模型无能——单条回复落库思考 43 万 / 28.7 万 /
14 万字符，正文却只有几十字符；全库 188 条带思考的回复里 21 条 >2 万字符。
这些思考被**每一轮原样回放**给模型（`build_model_history` 的 message 路径与
`_copy_model_trace_message` 的 trace 路径），模型看到自己上一轮的推理循环样本后被
强锚定，于是每轮都总结成同一条。

两处出口都必须挂闸门（两路互斥，但实测另有 23 条带思考却没有 trace 的老消息、
合计约 108 万字符只能走 message 路径）。只截**回放**，不动落库。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba import config as config_module  # noqa: E402
from naiba.core.history import (  # noqa: E402
    MODEL_REASONING_REPLAY_MAX_CHARS,
    MODEL_REASONING_REPLAY_MIN_KEEP_CHARS,
    MODEL_REASONING_REPLAY_OMITTED,
    MODEL_REASONING_REPLAY_TURN_CHARS,
    build_model_history,
)

MAINTENANCE_DOC = ROOT / "项目维护说明（修改代码前必读）.md"
OMITTED_PREFIX = MODEL_REASONING_REPLAY_OMITTED.split("{")[0]


def _read_source(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _assert_has(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} 缺少：{needle!r}")


def _reasoning_chars(message: dict) -> int:
    return len(str(message.get("reasoning_content") or ""))


def _trace_message(text: str, index: int) -> dict:
    """构造一条带思考的 trace 条目（assistant + reasoning_content）。"""
    return {"role": "assistant", "content": f"第{index}步", "reasoning_content": text}


def _turn(*texts: str) -> list[dict]:
    """把若干条思考包成一条 assistant 消息（trace 路径）。"""
    return [{"role": "assistant", "content": "最终答复", "metadata": {"trace": [
        _trace_message(text, index) for index, text in enumerate(texts)
    ]}}]


class ReasoningReplayClipTests(unittest.TestCase):
    """双闸门本体（单位 = 字符）。"""

    def test_single_entry_over_limit_is_clipped_with_marker(self) -> None:
        text = "思" * (MODEL_REASONING_REPLAY_MAX_CHARS + 1234)
        history = build_model_history(_turn(text))
        clipped = history[0]["reasoning_content"]
        self.assertTrue(clipped.startswith("思" * 100), "必须保留前缀（存在性契约要非空）")
        self.assertIn(OMITTED_PREFIX, clipped, "截断必须附省略标记")
        self.assertIn("1234", clipped, "标记里必须自述被省略的字符数")
        self.assertEqual(len(clipped), MODEL_REASONING_REPLAY_MAX_CHARS + len(
            MODEL_REASONING_REPLAY_OMITTED.format(n=1234)
        ))

    def test_single_entry_within_limit_is_untouched(self) -> None:
        text = "思" * MODEL_REASONING_REPLAY_MAX_CHARS
        history = build_model_history(_turn(text))
        self.assertEqual(history[0]["reasoning_content"], text, "恰好等于上限时不得截断")

    def test_message_path_is_also_gated(self) -> None:
        """没有 trace 的老消息只能走 message 路径（实测 23 条、约 108 万字符）。"""
        text = "想" * (MODEL_REASONING_REPLAY_MAX_CHARS * 3)
        history = build_model_history([
            {"role": "assistant", "content": "答复", "metadata": {"reasoning": [text]}},
        ])
        self.assertLess(len(history[0]["reasoning_content"]), len(text))
        self.assertIn(OMITTED_PREFIX, history[0]["reasoning_content"])

    def test_turn_budget_is_allocated_newest_first(self) -> None:
        """整轮软闸门：由新到旧分配，最新条目优先占满单条上限。"""
        texts = [f"{(index % 10)}{'思' * (MODEL_REASONING_REPLAY_MAX_CHARS - 1)}" for index in range(10)]
        history = build_model_history(_turn(*texts))
        reasonings = [item["reasoning_content"] for item in history]
        self.assertEqual(len(reasonings), 10)
        untouched_indexes = [
            index for index, value in enumerate(reasonings) if OMITTED_PREFIX not in value
        ]
        # 16000 / 4000 = 4 条占满额度，且必须是**最新**的那 4 条（下标 6..9）。
        self.assertEqual(untouched_indexes, [6, 7, 8, 9], "额度必须由新到旧分配")
        for index in range(6):
            self.assertIn(
                OMITTED_PREFIX, reasonings[index],
                "额度耗尽后更旧条目必须被压缩（保底也带标记）",
            )
        total = sum(len(value) for value in reasonings)
        floor_total = MODEL_REASONING_REPLAY_TURN_CHARS + 6 * MODEL_REASONING_REPLAY_MIN_KEEP_CHARS
        self.assertLessEqual(total, floor_total + 6 * len(OMITTED_PREFIX) + 200,
                             "整轮总量必须受控在预算 + 保底溢出内（软闸门允许标记开销）")

    def test_min_keep_floor_keeps_prefix(self) -> None:
        """保底不是「清零」：前缀 + 标记，仍然非空。"""
        texts = ["头" + "想" * 5000 for _ in range(8)]
        history = build_model_history(_turn(*texts))
        for item in history[6:]:
            value = item["reasoning_content"]
            self.assertTrue(value.startswith("头"), "保底条目仍要保留前缀")
            self.assertIn(OMITTED_PREFIX, value)

    def test_zero_disables_both_gates(self) -> None:
        text = "思" * 100000
        history = build_model_history(
            _turn(text, text),
            reasoning_replay_max_chars=0,
            reasoning_replay_turn_chars=0,
        )
        self.assertEqual(history[0]["reasoning_content"], text)
        self.assertEqual(history[1]["reasoning_content"], text)

    def test_turn_gate_alone_still_limits(self) -> None:
        """只关单条闸门、保留整轮预算：条目在额度耗尽后仍被压到保底。"""
        texts = ["思" * 20000 for _ in range(3)]
        history = build_model_history(
            _turn(*texts), reasoning_replay_max_chars=0, reasoning_replay_turn_chars=16000,
        )
        self.assertEqual(history[2]["reasoning_content"], texts[0], "最新条目不受限")
        self.assertIn(OMITTED_PREFIX, history[0]["reasoning_content"])
        self.assertLess(len(history[0]["reasoning_content"]), 300)

    def test_replay_is_deterministic(self) -> None:
        """同一输入 + 同一参数 ⇒ 逐字节一致（跨轮前缀缓存的硬契约）。"""
        messages = _turn("思" * 9000, "想" * 3000)
        first = build_model_history(messages)
        second = build_model_history(json.loads(json.dumps(messages, ensure_ascii=False)))
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )

    def test_non_text_fields_survive(self) -> None:
        """只压 reasoning_content：reasoning_id / tool_calls / content 一律不动。"""
        trace = [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "思" * 9000,
                "reasoning_id": "rs_123",
                "tool_calls": [{"id": "call_1", "name": "read_file", "arguments": {"path": "a"}}],
            },
            {"role": "tool", "content": "工具结果", "tool_call_id": "call_1", "name": "read_file"},
            {"role": "assistant", "content": "最终答复"},
        ]
        history = build_model_history([
            {"role": "assistant", "content": "最终答复", "metadata": {"trace": trace}}
        ])
        self.assertEqual(history[0]["reasoning_id"], "rs_123")
        self.assertEqual(history[0]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(history[1]["content"], "工具结果")
        self.assertEqual(history[2]["content"], "最终答复")
        self.assertIn(OMITTED_PREFIX, history[0]["reasoning_content"])

    def test_empty_and_missing_reasoning_are_unaffected(self) -> None:
        history = build_model_history([
            {"role": "assistant", "content": "无思考", "metadata": {}},
            {"role": "assistant", "content": "空思考", "metadata": {"reasoning": ""}},
            {"role": "user", "content": "你好"},
        ])
        self.assertNotIn("reasoning_content", history[0])
        self.assertNotIn("reasoning_content", history[1])
        self.assertEqual([item["content"] for item in history], ["无思考", "空思考", "你好"])

    def test_long_reasoning_does_not_change_answer_bytes(self) -> None:
        """限长不得碰到最终答复 / 上一条消息的正文（缓存只在被截那条之后重建）。"""
        history = build_model_history([
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "答复", "metadata": {"reasoning": ["想" * 20000]}},
        ])
        self.assertEqual(history[0]["content"], "问题")
        self.assertEqual(history[1]["content"], "答复")


class ReasoningPassbackBackfillTests(unittest.TestCase):
    """DeepSeek 官方 thinking 模式：每条 assistant 历史都必须带**非空** reasoning_content。

    病历（2026-10-01，用户截图真机 400）：`api.deepseek.com` 返回
    ``The reasoning_content in the thinking mode must be passed back to the API``。
    缺字段的消息来源不止一处——失败占位消息（``（本次回答未完成）``，首轮调用即 400 时
    trace/reasoning 都是空）、换模型前的老回复、无思考的工具轮。旧实现只保证"有思考就回放"，
    对"没思考可回放"的消息一律不带字段 ⇒ 400；且失败后落库的占位消息本身无思考，
    下一轮又 400（**毒化循环**：同一会话再也发不出去）。
    """

    DEEPSEEK = {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro"}
    OPENAI = {"base_url": "https://api.openai.com", "model": "gpt-5.6"}

    def _protocols(self):
        from naiba.llm.protocols import NO_REASONING_PLACEHOLDER, ProtocolMixins

        return ProtocolMixins, NO_REASONING_PLACEHOLDER

    def test_deepseek_profile_backfills_non_empty_placeholder(self) -> None:
        P, placeholder = self._protocols()
        kwargs = P._reasoning_passback_kwargs(self.DEEPSEEK, True)
        wire = P._openai_messages(
            [
                {"role": "user", "content": "问"},
                {"role": "assistant", "content": "（本次回答未完成）"},
                {"role": "assistant", "content": "有思考", "reasoning_content": "想"},
                {"role": "user", "content": "再问"},
            ],
            **kwargs,
        )
        self.assertEqual(wire[1]["reasoning_content"], placeholder, "缺思考必须补非空占位")
        self.assertTrue(wire[1]["reasoning_content"].strip(), "占位不得为空串")
        self.assertEqual(wire[2]["reasoning_content"], "想", "真思考不得被占位覆盖")
        self.assertNotIn("reasoning_content", wire[0], "user 消息不带字段")
        self.assertNotIn("reasoning_content", wire[3], "user 消息不带字段")

    def test_non_deepseek_profile_keeps_empty_backfill(self) -> None:
        P, placeholder = self._protocols()
        wire = P._openai_messages(
            [{"role": "assistant", "content": "答复"}],
            **P._reasoning_passback_kwargs(self.OPENAI, True),
        )
        self.assertEqual(wire[0]["reasoning_content"], "", "非 DeepSeek 保持原空串回填")
        self.assertNotEqual(wire[0]["reasoning_content"], placeholder)

    def test_reasoning_disabled_adds_no_field(self) -> None:
        """未开思考时不补任何字段（保持字节不变，避免给非思考端点塞字段）。"""
        P, _ = self._protocols()
        for profile in (self.DEEPSEEK, self.OPENAI):
            kwargs = P._reasoning_passback_kwargs(profile, False)
            self.assertEqual(kwargs, {"include_reasoning_content": False})
            wire = P._openai_messages([{"role": "assistant", "content": "答复"}], **kwargs)
            self.assertNotIn("reasoning_content", wire[0])

    def test_backfill_is_byte_stable_across_calls(self) -> None:
        """占位是常量 ⇒ 同一历史每轮字节一致（前缀缓存硬契约）。"""
        P, _ = self._protocols()
        message = [{"role": "assistant", "content": "（本次回答未完成）"}]
        first = P._openai_messages(message, **P._reasoning_passback_kwargs(self.DEEPSEEK, True))
        second = P._openai_messages(message, **P._reasoning_passback_kwargs(self.DEEPSEEK, True))
        self.assertEqual(json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))

    def test_failed_partial_message_is_backfilled_end_to_end(self) -> None:
        """端到端：失败占位消息经 build_model_history 重放后，wire 上必须带非空字段。"""
        P, placeholder = self._protocols()
        history = build_model_history([
            {"role": "user", "content": "你好"},
            {
                "role": "assistant",
                "content": "（本次回答未完成）",
                "metadata": {"partial": True, "reasoning": [], "trace": [], "error": "HTTP 400"},
            },
        ])
        wire = P._openai_messages(history, **P._reasoning_passback_kwargs(self.DEEPSEEK, True))
        self.assertEqual(wire[1]["content"], "（本次回答未完成）")
        self.assertEqual(wire[1]["reasoning_content"], placeholder)

    def test_empty_reasoning_string_is_also_backfilled(self) -> None:
        """空串思考（服务端返回过空 CoT）与缺字段同样被拒 ⇒ DeepSeek 下一并补占位。

        非 DeepSeek 画像保持原样（空串留在字段里，`include_reasoning_content` 的语义不变）。
        """
        P, placeholder = self._protocols()
        empty = [{"role": "assistant", "content": "答复", "reasoning_content": ""}]
        deepseek = P._openai_messages(empty, **P._reasoning_passback_kwargs(self.DEEPSEEK, True))
        self.assertEqual(deepseek[0]["reasoning_content"], placeholder, "空串过不了 DeepSeek 方言")
        other = P._openai_messages(empty, **P._reasoning_passback_kwargs(self.OPENAI, True))
        self.assertEqual(other[0]["reasoning_content"], "", "非 DeepSeek 保持原值（空串）")

    def test_both_wire_call_sites_share_the_helper(self) -> None:
        """两个 OpenAI Chat 形态调用点（openai_chat / lm_studio 带工具）必须共用同一裁决。"""
        source = _read_source("naiba", "llm", "runtime.py")
        self.assertEqual(
            source.count("_reasoning_passback_kwargs(profile, reasoning_enabled)"), 2,
            "两个 wire 调用点都要走 _reasoning_passback_kwargs，漏一处就出现两种字节",
        )
        self.assertNotIn(
            "include_reasoning_content=(\n", source,
            "旧的内联表达式必须已被收敛（否则两个调用点会漂移）",
        )

    def test_helper_is_the_single_decision_point(self) -> None:
        source = _read_source("naiba", "llm", "protocols.py")
        _assert_has(source, "def _reasoning_passback_kwargs(", "protocols.py 裁决点")
        _assert_has(source, "NO_REASONING_PLACEHOLDER", "protocols.py 占位常量复用")


class ReasoningReplayConfigTests(unittest.TestCase):
    """两个参数：默认值、校验、持久化、注入。"""

    def setUp(self) -> None:
        from server import ConfigStore

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.path.write_text(json.dumps({"providers": []}), encoding="utf-8")
        self.store = ConfigStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_match_module_constants(self) -> None:
        self.assertEqual(
            config_module.REASONING_REPLAY_MAX_CHARS_DEFAULT,
            MODEL_REASONING_REPLAY_MAX_CHARS,
            "config 与 core.history 的默认值必须一致（漂移会让默认值失效）",
        )
        self.assertEqual(
            config_module.REASONING_REPLAY_TURN_CHARS_DEFAULT,
            MODEL_REASONING_REPLAY_TURN_CHARS,
        )
        self.assertEqual(self.store.data["reasoning_replay_max_chars"], 4000)
        self.assertEqual(self.store.data["reasoning_replay_turn_chars"], 16000)
        public = self.store.public()
        self.assertEqual(public["reasoning_replay_max_chars"], 4000)
        self.assertEqual(public["reasoning_replay_turn_chars"], 16000)

    def test_zero_is_kept_and_blank_falls_back(self) -> None:
        self.store.update_settings({
            "reasoning_replay_max_chars": 0, "reasoning_replay_turn_chars": 0,
        })
        self.assertEqual(self.store.data["reasoning_replay_max_chars"], 0)
        self.assertEqual(self.store.data["reasoning_replay_turn_chars"], 0)
        self.store.update_settings({
            "reasoning_replay_max_chars": "", "reasoning_replay_turn_chars": "",
        })
        self.assertEqual(self.store.data["reasoning_replay_max_chars"], 4000)
        self.assertEqual(self.store.data["reasoning_replay_turn_chars"], 16000)

    def test_out_of_range_is_rejected(self) -> None:
        for bad in (-1, 99, 1000001, "abc"):
            with self.assertRaises(ValueError):
                self.store.update_settings({"reasoning_replay_max_chars": bad})
            with self.assertRaises(ValueError):
                self.store.update_settings({"reasoning_replay_turn_chars": bad})

    def test_options_helper_exposes_both(self) -> None:
        self.assertEqual(
            self.store.reasoning_replay_options(),
            {"reasoning_replay_max_chars": 4000, "reasoning_replay_turn_chars": 16000},
        )
        self.store.update_settings({"reasoning_replay_max_chars": 8000})
        self.assertEqual(
            self.store.reasoning_replay_options()["reasoning_replay_max_chars"], 8000
        )

    def test_helper_output_is_accepted_by_build_model_history(self) -> None:
        """签名契约：helper 的键必须能直接 **kwargs 进 build_model_history。"""
        history = build_model_history(
            _turn("思" * 20000), **self.store.reasoning_replay_options()
        )
        self.assertIn(OMITTED_PREFIX, history[0]["reasoning_content"])


class ReasoningReplayPlumbingTests(unittest.TestCase):
    """三个活调用点必须同参数（只加 KEEP 不够——漏一处就出现两种回放字节）。"""

    def test_all_live_call_sites_pass_the_options(self) -> None:
        for parts in (
            ("naiba", "run", "chat.py"),
            ("naiba", "subagent.py"),
            ("naiba", "plans.py"),
        ):
            source = _read_source(*parts)
            _assert_has(
                source, "reasoning_replay_options()",
                "思考回放限长透传（" + "/".join(parts) + "）",
            )

    def test_pure_function_accepts_the_two_parameters(self) -> None:
        source = _read_source("naiba", "core", "history.py")
        for token in ("reasoning_replay_max_chars", "reasoning_replay_turn_chars"):
            _assert_has(source, token, "core/history.py 参数")

    def test_settings_ui_exposes_both_fields(self) -> None:
        html = _read_source("public", "index.html")
        for token in ("reasoningReplayMaxChars", "reasoningReplayTurnChars"):
            _assert_has(html, token, "运行设置面板")
        script = _read_source("public", "js", "09-settings.js")
        for token in (
            "reasoning_replay_max_chars",
            "reasoning_replay_turn_chars",
        ):
            _assert_has(script, token, "设置页读写")

    def test_maintenance_doc_records_the_contract(self) -> None:
        doc = MAINTENANCE_DOC.read_text(encoding="utf-8")
        for token in (
            "MODEL_REASONING_REPLAY_MAX_CHARS",
            "reasoning_replay_max_chars",
            "_ReasoningReplayBudget",
        ):
            _assert_has(doc, token, "维护说明")

    def test_history_module_keeps_the_clip_helper_private(self) -> None:
        """截断实现必须只有一处（两处调用同一实现），否则两路会漂移。"""
        source = _read_source("naiba", "core", "history.py")
        self.assertEqual(
            source.count("MODEL_REASONING_REPLAY_OMITTED.format(n="), 1,
            "省略标记必须只在 _clip_reasoning_text 里拼装",
        )


if __name__ == "__main__":
    unittest.main()
