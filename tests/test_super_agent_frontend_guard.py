# -*- coding: utf-8 -*-
"""任务总管（super-agent）前端视觉守门：钉住 V2 视觉修复，防止回潮。

对应《2026-11-03-任务总管-V2-视觉优化计划》Q4（P-01/P-03），并顺带钉住
P-06（审核页不露内部细节）与 A15 已替换文案。纯源码断言：不启动浏览器、
不联网、不调用模型。

- P-01/P-02：内容框不得用硬编码浅底或未定义的 `--panel`（深色主题白底浅字）；
- P-03：任务总管样式块字号下限 11px（层级靠字重与 --muted，不靠 10px 灰字）；
- P-06：批次审核界面不得再渲染执行指纹、原始反推 JSON、内部任务 ID、绝对路径；
- A15：已替换的旧文案不得回潮（前端可见区域 + 官方方案描述）。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS_MARKER = "/* ============ 任务总管（Super-agent 控制面） ============ */"


def _css() -> str:
    return (ROOT / "public/styles.css").read_text(encoding="utf-8")


def _index() -> str:
    return (ROOT / "public/index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (ROOT / "public/js/28-super-agent.js").read_text(encoding="utf-8")


def _super_agent_css() -> str:
    css = _css()
    start = css.index(CSS_MARKER)
    return css[start:]


class SuperAgentStyleGuard(unittest.TestCase):
    def test_no_undefined_panel_variable(self):
        # `var(--panel, #fff)` 是未定义变量：深色主题下兜底成白色造成白底浅字（A07 根因）。
        self.assertNotIn("var(--panel", _css(), "styles.css 不得再使用未定义的 --panel 变量")

    def test_no_opaque_light_hardcoded_background_in_super_agent_block(self):
        offenders = []
        for line_number, line in enumerate(_super_agent_css().splitlines(), start=1):
            for declaration in re.finditer(r"background(?:-color)?:\s*([^;]+);", line):
                for color in re.findall(r"#[0-9a-fA-F]{3,8}\b", declaration.group(1)):
                    body = color[1:]
                    if len(body) == 3:
                        channels = [int(ch * 2, 16) for ch in body]
                        alpha = 255
                    elif len(body) == 4:
                        channels = [int(ch * 2, 16) for ch in body[:3]]
                        alpha = int(body[3] * 2, 16)
                    elif len(body) == 6:
                        channels = [int(body[i:i + 2], 16) for i in (0, 2, 4)]
                        alpha = 255
                    elif len(body) == 8:
                        channels = [int(body[i:i + 2], 16) for i in (0, 2, 4)]
                        alpha = int(body[6:8], 16)
                    else:
                        continue
                    if alpha >= 255 and min(channels) >= 240:
                        offenders.append(f"行 {line_number}：{color} → {line.strip()[:140]}")
        self.assertEqual(offenders, [], "任务总管样式块出现不透明浅色硬编码背景（深色主题会白底浅字）：\n" + "\n".join(offenders))

    def test_font_size_floor_11px_in_super_agent_block(self):
        offenders = []
        for line_number, line in enumerate(_super_agent_css().splitlines(), start=1):
            for match in re.finditer(r"font-size:\s*([\d.]+)px", line):
                if float(match.group(1)) < 11:
                    offenders.append(f"行 {line_number}：{match.group(0)} → {line.strip()[:140]}")
        self.assertEqual(offenders, [], "任务总管样式块出现低于 11px 的字号（层级应靠字重与 --muted）：\n" + "\n".join(offenders))

    def test_hidden_attribute_stays_hard_hidden(self):
        # styles.css 历史坑：作者样式会覆盖 [hidden]，任务总管必须保留 !important 显式规则。
        # 用 ID 指向弹窗（避免与 GhostDialogTests 的「dialog display 不得用 !important」相撞，
        # 该守门禁止的是弹层自身的 display 规则，不是隐藏内部 [hidden] 子元素）。
        css = _css()
        self.assertIn(".super-agent-view [hidden]", css)
        self.assertIn("#superAgentRecipeDialog [hidden]", css)
        self.assertIn("#superAgentCreateDialog [hidden]", css)


class SuperAgentCopyGuard(unittest.TestCase):
    def test_review_ui_hides_internal_details(self):
        source = _js()
        for phrase in ("模板来源指纹", "实际执行指纹", "原始反推结果", "提词任务 ${", "造梦任务 ${",
                       '<h3>${text(item.source_path)}', "recipeEffectiveItemMarkup"):
            self.assertNotIn(phrase, source, f"批次界面不得再渲染内部细节：{phrase}")

    def test_replaced_copy_does_not_regrow(self):
        frontend = _index() + _js()
        for phrase in ("图片 → 提示词", "有界配方批次", "核对并确认反推", "暂停后续派发",
                       "仅补收此项产物", "确认本次操作", "不能改稿后隐式重建"):
            self.assertNotIn(phrase, frontend, f"已替换的旧文案回潮：{phrase}")

    def test_recipe_selector_and_catalog_use_plain_copy(self):
        self.assertNotIn("请选择配方", _js())
        catalog = (ROOT / "naiba/super_agent/recipe_catalog.py").read_text(encoding="utf-8")
        for phrase in ("三字段", "六字段", "单首帧工作流"):
            self.assertNotIn(phrase, catalog, f"官方方案描述仍是技术化文案：{phrase}")
        recipes = (ROOT / "naiba/super_agent/recipes.py").read_text(encoding="utf-8")
        self.assertNotIn("有界配方批次", recipes)
        self.assertNotIn("隐式重建", recipes)


if __name__ == "__main__":
    unittest.main()
