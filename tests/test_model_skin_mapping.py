# -*- coding: utf-8 -*-
"""模型皮肤映射规则的单测（前端纯函数，用 node 跑）。

规则本体在 public/js/26-model-skin.js。它是纯函数、不碰 DOM，所以能直接 import 后断言，
不必起浏览器。为什么用 node 而不是重写一份 Python 版本：两套实现必然漂移，
守门要守的就是「真正跑在用户浏览器里的那份代码」。

覆盖计划里列的全部验证点：
- 版本号夹在中间不影响匹配（gpt-6.1-sol）
- 同族不同档位不被吞并（kimi-k3 vs kimi-k3-pro / MiniMax-M2 vs -Flash）
- 缺档回落（deepseek-v4-pro → 标准档）
- 无档位词默认偏高阶（gpt-5 / grok-4.7）
- 非对话模型排除（gpt-image-2 / qwen3-tts-flash / grok-imagine）
- 未知家族不换肤（mimo-v2.5 / Sakimichan）
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "public" / "js" / "26-model-skin.js"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("本机没有 node，跳过前端映射规则单测")
    return node


def resolve_many(model_ids: list[str]) -> dict[str, str]:
    """在 node 里 import 映射模块，批量解析模型名 → 皮肤键。"""
    script = (
        "import(process.env.SKIN_MOD).then((m) => {"
        "  const ids = JSON.parse(process.env.SKIN_IDS);"
        "  const out = {};"
        "  for (const id of ids) out[id] = m.resolveModelSkin(id);"
        "  process.stdout.write(JSON.stringify(out));"
        "});"
    )
    proc = subprocess.run(
        [_node(), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            "SKIN_MOD": MODULE.as_uri(),
            "SKIN_IDS": json.dumps(model_ids),
            "PATH": __import__("os").environ.get("PATH", ""),
            "SystemRoot": __import__("os").environ.get("SystemRoot", ""),
        },
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node 执行失败：{proc.stderr}")
    return json.loads(proc.stdout)


class ModelSkinMappingTest(unittest.TestCase):
    def _resolve(self, *model_ids: str) -> dict[str, str]:
        return resolve_many(list(model_ids))

    # ---- 版本号夹在中间 ----

    def test_version_number_in_the_middle_is_ignored(self):
        """`gpt-6.1-sol` 的 6.1 不参与判断：家族词 + 档位词决定结果。"""
        got = self._resolve("gpt-5.6-sol", "gpt-6.1-sol", "gpt-7.0-sol")
        self.assertEqual(
            {"gpt-5.6-sol": "07_gpt_sol_astra",
             "gpt-6.1-sol": "07_gpt_sol_astra",
             "gpt-7.0-sol": "07_gpt_sol_astra"},
            got,
        )

    def test_run_on_words_are_split(self):
        """`gemini31flash` 这种字母数字连写要拆开，否则 flash 匹配不到。"""
        got = self._resolve("gemini31flash", "gemini31flash-lite")
        self.assertEqual(
            {"gemini31flash": "04_gemini_flash", "gemini31flash-lite": "04_gemini_flash"},
            got,
        )

    # ---- 同族不同档位不被吞并 ----

    def test_tiers_are_not_swallowed_within_a_family(self):
        got = self._resolve(
            "kimi-k3", "kimi-k3-pro",
            "MiniMax-M2", "MiniMax-M2-Flash",
            "qwen3", "qwen-flash",
            "claude-opus-5.5", "claude-sonnet-4-5",
        )
        self.assertEqual("14_kimi", got["kimi-k3"])
        self.assertEqual("13_kimi_pro", got["kimi-k3-pro"])
        self.assertEqual("15_minimax_m", got["MiniMax-M2"])
        # 系列名 m 不是档位词，Flash 才是 → 必须落到轻量档。
        self.assertEqual("16_minimax_m_flash", got["MiniMax-M2-Flash"])
        self.assertEqual("17_qwen", got["qwen3"])
        self.assertEqual("18_qwen_flash", got["qwen-flash"])
        self.assertEqual("05_claude_opus_fable_mythos", got["claude-opus-5.5"])
        self.assertEqual("06_claude_haiku_sonnet", got["claude-sonnet-4-5"])

    # ---- 缺档回落 ----

    def test_high_tier_words_fall_back_to_standard_when_family_has_no_high_image(self):
        """DeepSeek / Qwen 没有高阶图，带 pro/max 词的模型回落标准档。"""
        got = self._resolve("deepseek-v4-pro", "deepseek-reasoner", "qwen-max", "qwen3-max")
        for model_id, skin in got.items():
            with self.subTest(model=model_id):
                self.assertIn(skin, {"01_deepseek", "17_qwen"})
        self.assertEqual("01_deepseek", got["deepseek-v4-pro"])
        self.assertEqual("17_qwen", got["qwen-max"])

    # ---- 无档位词默认偏高阶（产品拍板 2026-10-27） ----

    def test_models_without_tier_words_default_to_high_tier(self):
        got = self._resolve(
            "gpt-5", "gpt-4o", "grok-4.7", "grok-4.6", "claude-latest",
        )
        self.assertEqual("07_gpt_sol_astra", got["gpt-5"])
        self.assertEqual("07_gpt_sol_astra", got["gpt-4o"])
        self.assertEqual("09_grok_heavy", got["grok-4.7"])
        self.assertEqual("09_grok_heavy", got["grok-4.6"])
        self.assertEqual("05_claude_opus_fable_mythos", got["claude-latest"])

    def test_models_without_tier_words_use_standard_when_family_has_one(self):
        """有标准档的家族不受「偏高阶」影响，仍落标准档。"""
        got = self._resolve("glm-4.6", "deepseek-chat", "kimi-k2.7-code")
        self.assertEqual("11_glm", got["glm-4.6"])
        self.assertEqual("01_deepseek", got["deepseek-chat"])
        self.assertEqual("14_kimi", got["kimi-k2.7-code"])

    # ---- 非对话模型排除 ----

    def test_non_chat_models_do_not_change_skin(self):
        """画图 / 语音 / 视频模型也带家族词，但绝不能换肤。"""
        got = self._resolve(
            "gpt-image-2", "gpt-image-1.5", "qwen-image-2.0-pro",
            "qwen3-tts-flash", "grok-imagine", "grok-imagine-video-v1.5",
        )
        for model_id, skin in got.items():
            with self.subTest(model=model_id):
                self.assertEqual("", skin)

    # ---- 未知家族不猜测 ----

    def test_unknown_families_are_not_guessed(self):
        got = self._resolve(
            "mimo-v2.5", "doubao-1.5-pro", "mistral-large", "llama-3.3-70b",
            # 家族词必须独立命中：Sakimichan 含 "chan" 但绝不等于 claude。
            "Sakimichan", "SakimiChan",
            "", "   ",
        )
        for model_id, skin in got.items():
            with self.subTest(model=model_id):
                self.assertEqual("", skin)

    # ---- 供应商不参与判断 ----

    def test_same_model_from_different_providers_maps_identically(self):
        """映射只看模型 ID 本身，同族同档的两种写法结果一致。"""
        got = self._resolve("deepseek-v4.1-flash", "DeepSeek-V4.1-Flash", "deepseek_v4_1_flash")
        self.assertEqual({"02_deepseek_mini"}, set(got.values()))


class SkinMappingCoversEverySkinTest(unittest.TestCase):
    """18 个皮肤键每一个都必须有可达的模型名，避免出现「永远选不到」的死皮肤。"""

    def test_every_skin_key_is_reachable(self):
        from naiba.config import APPEARANCE_MODEL_SKIN_FILES

        probes = [
            "deepseek-chat", "deepseek-v4-flash",
            "gemini-3.1-pro", "gemini-3.8-flash",
            "claude-opus-5.5", "claude-haiku-4.5",
            "gpt-5.6-sol", "gpt-5.6-terra",
            "grok-4-heavy", "grok-2-mini",
            "glm-4.6", "glm-5.3-flash",
            "kimi-k3-pro", "kimi-k3",
            "MiniMax-M2", "MiniMax-M2-Flash",
            "qwen3", "qwen-flash",
        ]
        resolved = set(resolve_many(probes).values())
        self.assertEqual(
            set(APPEARANCE_MODEL_SKIN_FILES), resolved,
            "有皮肤的模型名映射不到，或映射到了不存在的皮肤键",
        )


if __name__ == "__main__":
    unittest.main()
