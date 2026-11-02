# -*- coding: utf-8 -*-
"""守门：安装/编辑 Skill 两个按钮发给 AI 的说明必须共用同一份「脚本规范」。

背景：附带脚本的 Skill 有两个硬性约定——脚本放 `scripts/` 子目录、文件读写显式 UTF-8
（冻结版 Python 默认编码是系统 locale，简体中文 Windows 上是 GBK，裸 `open()` 会乱码）。
这两条只在"发给 AI 的说明"里生效，因此必须保证：① 内容确实包含；② 安装/编辑两处
共用同一份常量（避免改一处忘另一处）；③ 编辑说明还要求顺手修正旧脚本的裸 open()。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class SkillPromptRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stream = (ROOT / "public" / "js" / "11-run-stream.js").read_text(encoding="utf-8")
        cls.chat = (ROOT / "public" / "js" / "12-chat-input.js").read_text(encoding="utf-8")
        cls.rules = (ROOT / "public" / "js" / "skill-script-rules.js").read_text(encoding="utf-8")

    def test_rules_constant_covers_both_conventions(self):
        self.assertIn("export const SKILL_SCRIPT_RULES", self.rules)
        self.assertNotIn("import ", self.rules)
        self.assertIn("scripts/ 子目录", self.rules)
        self.assertIn('encoding="utf-8"', self.rules)
        self.assertIn("GBK", self.rules, "必须说明裸 open() 在冻结版下按 GBK 读写")

    def test_both_prompts_reuse_the_shared_rules(self):
        self.assertIn("export const SKILL_INSTALL_PRESET", self.stream)
        self.assertIn("export const SKILL_EDIT_PRESET", self.chat)
        # 两处都以同一常量结尾（不各自抄一份）
        self.assertIn("+ SKILL_SCRIPT_RULES", self.stream)
        self.assertIn("+ SKILL_SCRIPT_RULES", self.chat)
        self.assertRegex(
            self.chat,
            r'import \{[^}]*SKILL_SCRIPT_RULES[^}]*\} from "\./skill-script-rules\.js"',
            "12-chat-input 必须从无依赖文案模块引入规范，不能立即读取循环依赖中的 const",
        )

        self.assertIn('export { SKILL_SCRIPT_RULES } from "./skill-script-rules.js"', self.stream)
        self.assertIn('import { SKILL_SCRIPT_RULES } from "./skill-script-rules.js"', self.stream)

    def test_install_prompt_requires_scripts_subdir_cleanup(self):
        self.assertIn("scripts/", self.stream)
        self.assertRegex(self.stream, r"移动到 scripts/|放到.*scripts/|放在.*scripts/")

    def test_edit_prompt_requires_fixing_bare_open(self):
        self.assertIn("裸用 open()", self.chat)
        self.assertIn('encoding="utf-8"', self.chat)


if __name__ == "__main__":
    unittest.main()
