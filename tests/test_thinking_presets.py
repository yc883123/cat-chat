# -*- coding: utf-8 -*-
"""守门：思考强度预设数据层（`naiba/llm/thinking.py`）。

来源：用户提出「每个 API 一套预设」。方向成立——它把 Naiba 原本用 if/else 硬扛的
「方言 × 档位词表」两层拆开，也是 A0 取 `ceiling` 的唯一来源。

三条纪律（抄骨架，不抄字面）：
1. **声明 ≠ 选择**；2. **未声明的档位在发请求前就失败**；3. **不跨适配器家族搬字段**。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.llm import thinking as T  # noqa: E402
from naiba.llm.protocols import ProtocolMixins as P  # noqa: E402

KIMI_K3 = {"model": "kimi-k3", "request_format": "openai_chat", "base_url": "https://api.moonshot.cn"}
KIMI_K2 = {"model": "kimi-k2.6", "request_format": "openai_chat", "base_url": "https://api.moonshot.cn"}
DEEPSEEK_CHAT = {
    "model": "deepseek-v4-flash", "request_format": "openai_chat",
    "base_url": "https://api.deepseek.com",
}
DEEPSEEK_CODEX = {**DEEPSEEK_CHAT, "request_format": "codex_responses"}
GPT = {"model": "gpt-4o", "request_format": "openai_chat", "base_url": "https://api.openai.com"}


class PresetResolutionTests(unittest.TestCase):
    """命中规则：卡片手填 > 模型名匹配的预设 > request_format 默认预设。"""

    def test_model_level_beats_protocol_family(self) -> None:
        self.assertEqual(T.resolve_thinking(KIMI_K3)["id"], "kimi_k3")
        self.assertEqual(T.resolve_thinking(KIMI_K2)["id"], "kimi_k2")
        self.assertEqual(T.resolve_thinking(DEEPSEEK_CHAT)["id"], "deepseek")
        self.assertEqual(T.resolve_thinking(DEEPSEEK_CODEX)["id"], "deepseek_codex")
        self.assertEqual(T.resolve_thinking(GPT)["id"], "openai")

    def test_source_marks_model_level_vs_format_level(self) -> None:
        """auto 档能不能填输出上限，全靠这个来源标记（A0 的粒度洞）。"""
        self.assertEqual(T.resolve_thinking(KIMI_K3)["source"], "model")
        self.assertEqual(T.resolve_thinking(DEEPSEEK_CHAT)["source"], "model")
        self.assertEqual(T.resolve_thinking(GPT)["source"], "format")
        self.assertEqual(
            T.resolve_thinking({"request_format": "codex_responses"})["source"], "format"
        )

    def test_qualification_constraint_requires_request_format(self) -> None:
        """kimi-k3 走 codex_responses 中继时不得命中 K3 预设，须落到 openai_codex。"""
        relay = {**KIMI_K3, "request_format": "codex_responses", "base_url": "https://relay.example.com"}
        resolved = T.resolve_thinking(relay)
        self.assertEqual(resolved["id"], "openai_codex")
        self.assertEqual(resolved["format"], T.THINKING_FORMAT_RESPONSES)

    def test_card_overrides_preset(self) -> None:
        card = {
            **GPT,
            "thinking": {
                "format": "openai",
                "efforts": {"off": None, "low": "xlow", "medium": "mid", "high": "ultra"},
                "max_output_ceiling": 65536,
            },
        }
        resolved = T.resolve_thinking(card)
        self.assertEqual(resolved["source"], "card")
        self.assertEqual(resolved["efforts"]["high"], "ultra", "网关自有词汇必须能直接配")
        self.assertEqual(resolved["max_output_ceiling"], 65536)

    def test_legacy_profile_without_card_zero_migration(self) -> None:
        """老 config 零迁移：没有 thinking 字段 ⇒ 完全走内置预设。"""
        self.assertNotIn("thinking", GPT)
        resolved = T.resolve_thinking(GPT)
        self.assertEqual(resolved["id"], "openai")
        self.assertEqual(resolved["efforts"]["high"], "high")
        self.assertEqual(resolved["max_output_ceiling"], 32768)

    def test_unknown_format_falls_back_to_empty(self) -> None:
        resolved = T.resolve_thinking({"request_format": "newfangled"})
        self.assertEqual(resolved["max_output_ceiling"], 0, "未知协议不得凭空填上限")
        self.assertEqual(T.thinking_payload(resolved, "high"), {})

    def test_local_backends_are_never_capped(self) -> None:
        """本地后端的上限由后端自己决定：预设 ceiling 必须是 0（不注入）。"""
        for fmt in ("ollama", "lm_studio"):
            self.assertEqual(T.max_output_ceiling(T.resolve_thinking({"request_format": fmt})), 0)

    def test_kimi_k3_ceiling_pinned_to_measured_value(self) -> None:
        """K3 上限钉死实测值 1048576（2026-09-19 teynex 中继实测 + 官方文档口径一致；

        实测：4096~1048576 全阶梯 200，10M 撞的是计费预扣墙而非协议拒绝，
        int32 上限被端点判 invalid）。不准静默改回 0（= 放弃 A0 兜底）。
        """
        resolved = T.resolve_thinking(KIMI_K3)
        self.assertEqual(resolved["id"], "kimi_k3")
        self.assertEqual(resolved["max_output_ceiling"], 1048576)


class UndeclaredEffortTests(unittest.TestCase):
    """纪律 2：未声明的档位在发请求前就失败（不是发出去等 400）。"""

    def test_undeclared_effort_raises_before_request(self) -> None:
        preset = {
            "id": "narrow",
            "format": T.THINKING_FORMAT_OPENAI,
            "efforts": {"off": None, "low": "low", "high": "high"},
        }
        self.assertEqual(T.thinking_payload(preset, "low"), {"reasoning_effort": "low"})
        with self.assertRaises(T.ThinkingConfigError):
            T.thinking_payload(preset, "medium")
        # 旧入口保持容错语义（不错杀既有调用点）
        self.assertEqual(T.thinking_payload(preset, "medium", strict=False), {})

    def test_card_without_efforts_is_rejected(self) -> None:
        with self.assertRaises(T.ThinkingConfigError):
            T.resolve_thinking({**GPT, "thinking": {"format": "openai"}})

    def test_unknown_card_format_is_rejected(self) -> None:
        with self.assertRaises(T.ThinkingConfigError):
            T.resolve_thinking({**GPT, "thinking": {"format": "made-up", "efforts": {}}})

    def test_unknown_card_effort_key_is_rejected(self) -> None:
        with self.assertRaises(T.ThinkingConfigError):
            T.resolve_thinking({**GPT, "thinking": {"efforts": {"ultra": "ultra"}}})

    def test_negative_ceiling_is_rejected(self) -> None:
        with self.assertRaises(T.ThinkingConfigError):
            T.resolve_thinking({**GPT, "thinking": {"max_output_ceiling": -1}})

    def test_builtin_presets_declare_all_four_efforts(self) -> None:
        """内置预设必须四档齐全，否则用户一调档就报错（内置表不允许有洞）。"""
        for preset in T.THINKING_PRESETS:
            self.assertEqual(
                set(preset["efforts"]), set(T.THINKING_EFFORT_KEYS),
                f"预设 {preset['id']} 的档位不全",
            )


class EffortMappingTests(unittest.TestCase):
    """§九.102 三家分治的语义不变（只把承载方式从 if/else 换成预设数据）。"""

    def test_kimi_k3_four_step_mapping(self) -> None:
        preset = T.resolve_thinking(KIMI_K3)
        self.assertEqual(T.thinking_payload(preset, "off"), {"reasoning_effort": "low"})
        self.assertEqual(T.thinking_payload(preset, "low"), {"reasoning_effort": "low"})
        self.assertEqual(T.thinking_payload(preset, "medium"), {"reasoning_effort": "high"})
        self.assertEqual(T.thinking_payload(preset, "high"), {"reasoning_effort": "max"})
        self.assertEqual(T.thinking_payload(preset, "auto"), {})

    def test_deepseek_codex_mapping(self) -> None:
        preset = T.resolve_thinking(DEEPSEEK_CODEX)
        self.assertEqual(T.thinking_payload(preset, "off"), {"reasoning": {"effort": "none"}})
        self.assertEqual(T.thinking_payload(preset, "high"), {"reasoning": {"effort": "max"}})

    def test_kimi_k2_sends_nothing(self) -> None:
        """根因 6 的真 bug：K2 收到 reasoning_effort 会 400，改成数据后一个字段都不发。"""
        preset = T.resolve_thinking(KIMI_K2)
        for effort in T.THINKING_EFFORT_KEYS:
            self.assertEqual(T.thinking_payload(preset, effort), {})

    def test_deepseek_chat_sends_nothing(self) -> None:
        preset = T.resolve_thinking(DEEPSEEK_CHAT)
        for effort in T.THINKING_EFFORT_KEYS:
            self.assertEqual(T.thinking_payload(preset, effort), {})

    def test_ollama_off_is_false_not_absent(self) -> None:
        """ollama 的 off 是布尔 False（有意义的值），不能按「假值即不发」处理。"""
        preset = T.resolve_thinking({"request_format": "ollama"})
        self.assertEqual(T.thinking_payload(preset, "off"), {"think": False})
        self.assertEqual(T.thinking_payload(preset, "high"), {"think": "high"})

    def test_lm_studio_dialect(self) -> None:
        preset = T.resolve_thinking({"request_format": "lm_studio"})
        self.assertEqual(T.thinking_payload(preset, "off"), {"reasoning": "off"})
        self.assertEqual(T.thinking_payload(preset, "medium"), {"reasoning": "medium"})

    def test_protocol_mixin_delegates_to_presets(self) -> None:
        self.assertEqual(P._reasoning_params("openai_chat", "high"), {"reasoning_effort": "high"})
        self.assertEqual(
            P._reasoning_params("openai_chat", "high", kimi_k3=True),
            {"reasoning_effort": "max"},
        )
        self.assertEqual(P._reasoning_params("openai_chat", "high", deepseek=True), {})
        self.assertEqual(P._is_kimi_k3_profile({"model": "Kimi-K3-0905"}), True)
        self.assertEqual(P._is_kimi_k3_profile({"model": "kimi-k2.6"}), False)


class LowerEffortChainTests(unittest.TestCase):
    """降档链必须**按词表声明顺序**下探，而不是「应用四档减一」。"""

    def test_k3_lowers_by_declared_order(self) -> None:
        preset = T.resolve_thinking(KIMI_K3)
        chain = ["high"]
        while True:
            nxt = T.lower_effort(preset, chain[-1])
            if not nxt:
                break
            chain.append(nxt)
        self.assertEqual(chain, ["high", "medium", "low"])
        # 线上值：max → high → low（medium 档的线上值就是 high）
        self.assertEqual(
            [T.thinking_payload(preset, item)["reasoning_effort"] for item in chain],
            ["max", "high", "low"],
        )

    def test_step_skips_undeclared_middle(self) -> None:
        """只声明 low/high 的词表：从 high 直接退到 low，不退到不存在的中档。"""
        preset = {
            "id": "two-step",
            "format": T.THINKING_FORMAT_OPENAI,
            "efforts": {"off": None, "low": "low", "high": "high"},
        }
        self.assertEqual(T.lower_effort(preset, "high"), "low")
        self.assertEqual(T.lower_effort(preset, "low"), "")

    def test_low_and_off_and_auto_cannot_lower(self) -> None:
        preset = T.resolve_thinking(GPT)
        self.assertEqual(T.lower_effort(preset, "low"), "", "退到 off（不发字段）不算降档")
        self.assertEqual(T.lower_effort(preset, "off"), "")
        self.assertEqual(T.lower_effort(preset, "auto"), "")

    def test_single_declared_effort_cannot_lower(self) -> None:
        preset = {"id": "one", "format": T.THINKING_FORMAT_OPENAI, "efforts": {"high": "high"}}
        self.assertEqual(T.lower_effort(preset, "high"), "")

    def test_protocol_mixin_lowering_uses_profile_preset(self) -> None:
        self.assertEqual(P._lower_reasoning_effort("high", KIMI_K3), "medium")
        self.assertEqual(P._lower_reasoning_effort("medium", KIMI_K3), "low")
        self.assertEqual(P._lower_reasoning_effort("low", KIMI_K3), "")
        self.assertEqual(P._lower_reasoning_effort("high", DEEPSEEK_CHAT), "")


class ThinkingActiveTests(unittest.TestCase):
    """A0 的填充判据：必须是**模型级**证据，协议族预设命中不构成依据。"""

    def test_explicit_efforts_always_active(self) -> None:
        for effort in ("low", "medium", "high"):
            self.assertTrue(T.thinking_active(GPT, effort))

    def test_off_never_active(self) -> None:
        self.assertFalse(T.thinking_active(GPT, "off"))
        self.assertFalse(T.thinking_active(DEEPSEEK_CHAT, "off"))

    def test_auto_requires_model_level_evidence(self) -> None:
        self.assertFalse(T.thinking_active(GPT, "auto"), "gpt-4o 不思考：协议族预设不算证据")
        self.assertFalse(
            T.thinking_active({"request_format": "codex_responses"}, "auto"),
            "openai_codex 是协议族预设，不能证明这台模型会思考",
        )
        self.assertTrue(T.thinking_active(DEEPSEEK_CHAT, "auto"))
        self.assertTrue(T.thinking_active(KIMI_K3, "auto"))

    def test_card_declaration_counts_as_evidence(self) -> None:
        self.assertTrue(T.thinking_active({**GPT, "thinking": {"max_output_ceiling": 8192}}, "auto"))

    def test_strip_thinking_fields_removes_every_dialect(self) -> None:
        payload = {"model": "m", "reasoning_effort": "max", "reasoning": {"effort": "max"},
                   "think": True, "thinking": {"x": 1}, "max_tokens": 5}
        stripped = T.strip_thinking_fields(payload)
        self.assertEqual(stripped, {"model": "m", "max_tokens": 5})
        self.assertIn("reasoning_effort", payload, "strip 必须返回副本，不改原负载")


if __name__ == "__main__":
    unittest.main()
