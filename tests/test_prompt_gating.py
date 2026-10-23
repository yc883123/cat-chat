# -*- coding: utf-8 -*-
"""守门：系统提示里的视觉/ComfyUI 编排指引必须与会话固化工具集同口径（缺工具就不提）。

背景：这两段原本无条件注入——工具集里没有对应工具时，系统提示等于让模型去调用不存在的
工具；而会话工具集首轮固化、中途不可改，用户只能重开会话换 Agent（PDF 段已按同一口径
处理）。判定只依赖会话固化的工具集（同一会话内恒定），不按"本轮是否含图"这类逐轮状态，
因此不会破坏前缀缓存。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.run.chat import (  # noqa: E402
    VISION_ANALYZE_GUIDE,
    VISION_ANALYZE_LOAD_GUIDE,
    VISION_OPS_GUIDE,
    vision_prompt_sections,
)
from naiba.skills.agent import (  # noqa: E402
    BACKGROUND_JOB_THRESHOLD_SECONDS,
    background_creator_rule,
    comfyui_script_guide_enabled,
    comfyui_script_guide_text,
)


class VisionPromptGatingTests(unittest.TestCase):
    def test_no_vision_tools_no_section(self):
        for has_vision in (False, True):
            with self.subTest(model_has_vision=has_vision):
                self.assertEqual(
                    vision_prompt_sections({"read_file", "pwsh"}, model_has_vision=has_vision), [])

    def test_text_model_gets_analyze_wording(self):
        parts = vision_prompt_sections({"vision_analyze"}, model_has_vision=False)
        self.assertEqual(parts, [VISION_ANALYZE_GUIDE])
        self.assertNotIn("vision_image_ops", "".join(parts))
        self.assertIn("需要了解附件/上下文中图片的内容时", "".join(parts))
        self.assertNotIn("装入本次对话", "".join(parts), "分析形态不得写成装载语义")

    def test_multimodal_model_gets_load_wording(self):
        """vision_analyze 的 schema 按模型能力分流（分析/装载），文案必须同口径。"""
        parts = vision_prompt_sections({"vision_analyze"}, model_has_vision=True)
        self.assertEqual(parts, [VISION_ANALYZE_LOAD_GUIDE])
        self.assertIn("装入本次对话", "".join(parts), "多模态形态应说明是把图片装入对话")
        self.assertNotIn("视觉后端", "".join(parts))

    def test_analyze_plus_ops_appends_ops_sentence(self):
        parts = vision_prompt_sections({"vision_analyze", "vision_image_ops"}, model_has_vision=False)
        self.assertEqual(parts, [VISION_ANALYZE_GUIDE, VISION_OPS_GUIDE])
        self.assertEqual("".join(parts), (
            "图片处理策略：需要了解附件/上下文中图片的内容时，调用 vision_analyze 工具并传入图片路径。"
            "仅当用户明确要求裁剪、OCR、坐标、像素比较等新操作时才调用 vision_image_ops。"
        ))

    def test_text_guide_has_no_multimodal_clause(self):
        """文本模型看不到原图，不该出现"（多模态模型）无需调用"这类描述别的模型的从句。"""
        self.assertNotIn("多模态模型", VISION_ANALYZE_GUIDE)
        self.assertNotIn("无需调用", VISION_ANALYZE_GUIDE)

    def test_load_plus_ops_appends_ops_sentence(self):
        parts = vision_prompt_sections({"vision_analyze", "vision_image_ops"}, model_has_vision=True)
        self.assertEqual(parts, [VISION_ANALYZE_LOAD_GUIDE, VISION_OPS_GUIDE])

    def test_ops_only_gets_standalone_sentence(self):
        for has_vision in (False, True):
            with self.subTest(model_has_vision=has_vision):
                parts = vision_prompt_sections({"vision_image_ops"}, model_has_vision=has_vision)
                self.assertEqual(len(parts), 1, "只开 ops 也要给一句，否则模型不知道它何时用")
                self.assertTrue(parts[0].startswith("图片处理策略："))
                self.assertIn("vision_image_ops", parts[0])
                self.assertNotIn("vision_analyze", parts[0])


class ComfyuiGuideGatingTests(unittest.TestCase):
    def test_requires_comfyui_tool(self):
        self.assertFalse(comfyui_script_guide_enabled({"write_file", "pwsh", "run_in_background"}))
        self.assertFalse(comfyui_script_guide_enabled({"read_file", "vision_analyze"}))

    def test_builtin_comfyui_tools_enable_guide(self):
        self.assertTrue(comfyui_script_guide_enabled({"comfyui_batch", "pwsh"}))
        self.assertTrue(comfyui_script_guide_enabled({"comfyui_prepare_workflow", "write_file"}))

    def test_mcp_comfy_tool_enables_guide(self):
        self.assertTrue(comfyui_script_guide_enabled(
            {"mcp__comfy-mcp__run_workflow", "run_in_background"}))

    def test_requires_script_path_tools_too(self):
        self.assertFalse(comfyui_script_guide_enabled({"comfyui_batch", "read_file"}),
                         "指引引用了 write_file/pwsh/run_in_background，缺了就不该提")

    def test_unrelated_mcp_tool_does_not_enable(self):
        self.assertFalse(comfyui_script_guide_enabled({"mcp__other__ping", "pwsh"}))

    def test_guide_text_names_only_available_executors(self):
        """文案只讲「脚本怎么写」，**不点名执行入口**（执行由后台任务规则唯一裁定）。

        计划 2026-10-22 §3.1：「脚本可用 pwsh 或 run_in_background 执行」是含糊表述——
        编写与执行是两个决定。执行入口统一交给 ``background_creator_rule``（见本类下方
        的断言），所以这里的文案不得再出现 pwsh / run_in_background。
        """
        both = comfyui_script_guide_text({"comfyui_batch", "pwsh", "run_in_background", "write_file"})
        self.assertNotIn("pwsh", both, "执行入口不在编排指引里点名")
        self.assertNotIn("run_in_background", both, "执行入口不在编排指引里点名")
        self.assertIn("write_file", both, "写文件工具可用时点名它（生成脚本的唯一入口）")
        only_pwsh = comfyui_script_guide_text({"comfyui_batch", "pwsh"})
        self.assertIn("脚本", only_pwsh)
        self.assertNotIn("write_file", only_pwsh,
                         "工具集里没有 write_file 时不得点名它")
        only_bg = comfyui_script_guide_text({"comfyui_batch", "run_in_background"})
        self.assertIn("脚本", only_bg)
        self.assertNotIn("run_in_background", only_bg)
        # 执行入口的唯一出处：同一组工具下，后台任务规则给出 30 秒阈值与固定 shell 样例。
        rule = background_creator_rule({"run_in_background", "pwsh", "comfyui_batch"})
        self.assertIn(str(BACKGROUND_JOB_THRESHOLD_SECONDS), rule, "阈值必须出现（30 秒口径）")
        self.assertIn("comfyui_batch", rule, "已有 ComfyUI 工作流走 comfyui_batch")
        self.assertIn('"kind": "shell"', rule, "唯一固定样例要教 shell Job 的 spec 结构")
        self.assertEqual(comfyui_script_guide_text({"read_file"}), "",
                         "不满足条件时返回空串（调用点按空串跳过注入）")


class BackgroundCreatorGatingTests(unittest.TestCase):
    """后台创建指引只点名**真实可用**的创建器（计划 2026-10-20 §3.3.1）。

    旧写法是「只要 comfyui_batch / 子 Agent 工具在就提 run_in_background」——工具集里
    没有 run_in_background 的会话（只读模式、提示词优化 Agent、升级前已固化的老会话）
    会被系统提示指引去调一个不存在的工具。
    """

    def test_no_creator_no_rule(self):
        for allowed in (set(), {"read_file", "vision_analyze"}, {"write_file", "pwsh"}):
            with self.subTest(allowed=sorted(allowed)):
                self.assertEqual(background_creator_rule(allowed), "",
                                 "没有创建器就一个字都不提")

    def test_rule_names_only_present_creators(self):
        rule = background_creator_rule({"run_in_background", "job_output", "job_status",
                                        "job_wait", "job_kill"})
        self.assertIn("run_in_background", rule)
        for absent in ("comfyui_batch", "subagent", "subagent_spawn"):
            self.assertNotIn(absent, rule, "不得点名本会话不存在的创建器")

    def test_comfyui_only_session_does_not_mention_run_in_background(self):
        """只有 comfyui_batch 的会话：这条规则只提它自己。"""
        rule = background_creator_rule({"comfyui_batch", "job_output", "job_status", "job_wait"})
        self.assertIn("comfyui_batch", rule)
        self.assertNotIn("run_in_background", rule)

    def test_subagent_only_session_mentions_only_subagent(self):
        rule = background_creator_rule({"subagent", "job_output"})
        self.assertIn("subagent", rule)
        self.assertNotIn("subagent_spawn", rule, "两种模式互斥，只提被启用的那个")
        self.assertNotIn("run_in_background", rule)

    def test_old_wide_condition_is_gone(self):
        """源码级：旧的无条件点名文案与过宽条件不得复活。"""
        source = (ROOT / "naiba/skills/agent.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "需要后台任务时必须先调用 run_in_background、comfyui_batch 或子 Agent 工具",
            source,
        )
        self.assertNotIn(
            'if {"run_in_background", "comfyui_batch", "subagent", "subagent_spawn"} & allowed:',
            source,
            "过宽的「只要有一创建器就提全部」条件不得复活",
        )
        self.assertIn("background_creator_rule(allowed)", source)


class VisionVariantConsistencyTests(unittest.TestCase):
    """文案与 schema 必须同口径：装载形态的 schema 说「装入本次对话」，文案也得这么说。"""

    def test_guide_matches_schema_variant_wording(self):
        from naiba.tools.registry import build_vision_tool_specs, vision_analyze_load_variant

        base = next(spec for spec in build_vision_tool_specs() if spec.name == "vision_analyze")
        load = vision_analyze_load_variant(base)
        self.assertIn("装入本次对话", load.description, "多模态形态的 schema 是装载语义")
        self.assertIn("视觉后端", base.description, "文本形态的 schema 是分析语义")
        self.assertIn("装入本次对话", VISION_ANALYZE_LOAD_GUIDE, "装载文案必须与装载 schema 同口径")
        self.assertNotIn("装入本次对话", VISION_ANALYZE_GUIDE, "分析文案不得写成装载语义")


class PromptSourceWiringTests(unittest.TestCase):
    """源码级：两处提示必须走上面的判定函数，不得再无条件拼接。"""

    def test_chat_uses_vision_sections(self):
        source = (ROOT / "naiba/run/chat.py").read_text(encoding="utf-8")
        self.assertIn("vision_prompt_sections(", source)
        self.assertIn("if vision_sections:", source, "视觉段必须条件拼接")
        self.assertIn("model_has_vision=bool(brain_supports_images)", source,
                      "视觉文案必须按会话模型能力分流（与 session_tool_defs 同口径）")

    def test_agent_uses_comfyui_gate(self):
        source = (ROOT / "naiba/skills/agent.py").read_text(encoding="utf-8")
        self.assertIn("if comfyui_script_guide_enabled(allowed):", source)
        self.assertIn("comfyui_script_guide_text(allowed)", source,
                      "文案必须走同一个门（否则过门的那句话里仍可能点名不存在的工具）")
        self.assertNotIn('if {"write_file", "pwsh", "run_in_background"} & allowed:', source,
                         "旧的“有写文件/命令就提 ComfyUI”条件不得复活")

    def test_show_media_prompt_is_tool_gated(self):
        """指名出卡的编排规则只在 `show_media` 真在工具集里时注入（不破前缀缓存）。"""
        source = (ROOT / "naiba/run/chat.py").read_text(encoding="utf-8")
        self.assertIn('if "show_media" in allowed_tools:', source)
        section = source[source.index('if "show_media" in allowed_tools:'):]
        section = section[: section.index(".strip()")]
        for phrase in ("show_media", "list_directory", "不要为了满足展示请求去扫描目录"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section, "提示段要写清用途、边界与禁止事项")


if __name__ == "__main__":
    unittest.main()
