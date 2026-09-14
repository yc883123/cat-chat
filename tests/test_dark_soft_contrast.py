# -*- coding: utf-8 -*-
"""暗色主题必须覆盖「换肤软色」变量的守门。

背景（2026-09-14 用户反馈两张截图）：暗色主题下有两处文字看不清——
「当前对话有 N 个 Run 正在执行」（`.active-task-bar`）与设置里 API 卡片的
「当前」那张的卡片名（`.provider-card.is-default`）。

根因不在这些组件本身：`:root` 与 `html[data-skin="…"]` 把 `--accent-soft` /
`--accent-soft-2` / `--violet-*` 定义成**服务于浅色主题的浅底淡彩**（如 `#EDECFF`、
`#d9efff`），而 `html[data-theme="dark"]` 只覆盖了 `--neutral-*` / `--text` 等，
**没有重定义这些软色**；`--text` 已变成近白 ⇒ 近白底 + 近白字的组合。
浏览器实测（`verify/_check_dark_soft_contrast.cjs`）：修复前该组合对比度 **1.05**
（1.0 = 完全同色），修复后 16.92。

本测试不跑浏览器，只钉住两条易复发的不变量：
1. 暗色块必须覆盖每个皮肤定义的换肤软色变量（新增皮肤/新增软色变量时不再漏）；
2. 暗色下的覆盖值必须是**重新混色**（`color-mix`），不能照抄浅色值或指向浅色别名。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS_PATH = ROOT / "public" / "styles.css"
SKINS = ("ocean", "rose", "forest")

# 暗底上必须重新混色的变量：软色底（做背景）+ violet deep（做暗底上的文字色）。
# `--violet-line` 有意不在此列——它是描边色，亮一点反而让用户气泡边界更清晰。
SOFT_BG_VARS = ("--accent-soft", "--accent-soft-2", "--violet-soft", "--violet-softer")
DEEP_TEXT_VARS = ("--violet-deep", "--violet-deep-2")


def _block(css: str, selector: str) -> str:
    """取 `selector { … }` 的正文。本项目每条主题/皮肤规则都在单行内闭合，块内无嵌套规则。"""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    if match is None:
        raise AssertionError(f"styles.css 里找不到规则块：{selector}")
    return match.group(1)


def _declared(block: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))


class DarkSoftColorGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = CSS_PATH.read_text(encoding="utf-8")
        cls.dark = _block(cls.css, 'html[data-theme="dark"]')

    def test_dark_covers_every_skin_soft_var(self) -> None:
        """皮肤里定义的每个「…soft…」变量，暗色块都要有对应覆盖。"""
        skin_soft: set[str] = set()
        for skin in SKINS:
            block = _block(self.css, f'html[data-skin="{skin}"]')
            skin_soft |= {name for name in _declared(block) if "soft" in name}
        self.assertTrue(skin_soft, "皮肤块里没找到软色变量，解析可能已失效")
        missing = sorted(skin_soft - _declared(self.dark))
        self.assertEqual(missing, [], f"暗色主题缺少换肤软色覆盖：{missing}")

    def test_dark_covers_soft_background_and_deep_text_vars(self) -> None:
        """两种角色的变量都要覆盖：软色底（背景）与 violet deep（暗底上的文字色）。"""
        declared = _declared(self.dark)
        missing = [name for name in SOFT_BG_VARS + DEEP_TEXT_VARS if name not in declared]
        self.assertEqual(missing, [], f"暗色主题缺少软色覆盖：{missing}")

    def test_dark_values_are_remixed(self) -> None:
        """覆盖值必须重新混色，不能照抄浅色值（照抄 == 没覆盖）。"""
        for name in SOFT_BG_VARS + DEEP_TEXT_VARS:
            with self.subTest(var=name):
                match = re.search(rf"{re.escape(name)}\s*:\s*([^;}}]+)", self.dark)
                self.assertIsNotNone(match, f"{name} 在暗色块里缺少声明")
                value = match.group(1).strip()
                self.assertIn("color-mix", value,
                              f"{name} 的暗色值必须重新混色，当前为 {value!r}")

    def test_consumers_read_the_variables(self) -> None:
        """两个出问题的组件必须用变量取色（这样暗色适配只需改变量，不必改组件）。"""
        for selector in (".active-task-bar", ".provider-card.is-default"):
            with self.subTest(selector=selector):
                block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
                self.assertIsNotNone(block, f"styles.css 里找不到规则：{selector}")
                self.assertIn("--accent-soft", block.group(1),
                              f"{selector} 的背景应取 var(--accent-soft)")


if __name__ == "__main__":
    unittest.main()
