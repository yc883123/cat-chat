# -*- coding: utf-8 -*-
"""护栏：dsh-genui 移植（方案 A）+ 下拉框重复 ▼ 修复 + 思考强度菜单重做。"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


class GenuiAndSelectPolishTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行 GenUI 行为验证")
    def test_genui_routes_only_closed_assistant_cards_and_preserves_aux_draft(self):
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "genui_frontend.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_genui_vendor_bundle_and_assets_present(self):
        vendor = ROOT / "public" / "vendor" / "genui"
        self.assertTrue((vendor / "cat-genui.js").is_file(), "核心渲染器 cat-genui.js 必须存在")
        self.assertTrue((vendor / "LICENSE").is_file(), "MIT LICENSE 必须随包保留")
        self.assertTrue((vendor / "THIRD_PARTY_NOTICES.md").is_file())
        assets = vendor / "assets"
        for name in (
            "standalone-runtime.js",
            "echarts-core.js",
            "echarts-full.js",
            "icons.js",
            "mermaid.js",
            "three.js",
            "maplibre.js",
        ):
            with self.subTest(asset=name):
                self.assertTrue((assets / name).is_file(), f"缺少懒加载引擎：{name}")

    def test_genui_builtin_skill_present_and_inline_adapted(self):
        skill = ROOT / "skills" / "genui" / "SKILL.md"
        self.assertTrue(skill.is_file(), "内置 genui Skill 必须存在")
        text = skill.read_text(encoding="utf-8")
        self.assertIn("name: genui", text)
        self.assertIn("dsh-ui", text)
        self.assertIn("Cat Chat", text)
        self.assertTrue((ROOT / "skills" / "genui" / "ICON-NAMES.md").is_file())

    def test_genui_frontend_wiring(self):
        md = _read("public", "js", "02-markdown.js")
        bridge = _read("public", "js", "33-genui.js")
        core = _read("public", "js", "01-core.js")
        bind = _read("public", "js", "15-bind-events.js")
        css = _read("public", "styles.css")
        self.assertIn("export function isGenuiFence", md)
        self.assertIn("data-genui-fence", md)
        self.assertIn("/vendor/genui/cat-genui.js", bridge)
        self.assertIn("/vendor/genui/assets/", bridge)
        self.assertIn("bindGenuiObserver", bridge)
        self.assertIn("data-ds-dark-theme", core)
        self.assertIn("bindGenuiObserver()", bind)
        self.assertIn("--dsw-alias-state-success-primary", css)
        self.assertIn(".genui-slot.is-rendered .genui-fallback", css)

    def test_select_picker_icon_hidden_and_workspace_select_keeps_caret(self):
        css = _read("public", "styles.css")
        self.assertIn("select:not([multiple]):not([size])::picker-icon", css)
        self.assertIn("#composerWorkspaceSelect:not([multiple]):not([size])", css)
        self.assertNotIn("#composerWorkspaceSelect { border: 0; outline: 0; background: var(--surface);", css)

    def test_reasoning_menu_and_hollow_context_ring(self):
        html = _read("public", "index.html")
        css = _read("public", "styles.css")
        self.assertIn('class="reasoning-menu-head"', html)
        for effort in ("auto", "off", "low", "medium", "high"):
            self.assertIn(f'data-reasoning-effort="{effort}"', html)
        self.assertIn("mask-image: radial-gradient(farthest-side, transparent", css)
        # 思考强度胶囊位于 .composer-meta-left 内、紧跟审批模式，不再放在 .composer 输入栏里
        composer_form = html[html.index('<form class="composer"'):html.index('</form>')]
        self.assertNotIn('id="deepReasoningButton"', composer_form)
        meta_left = html[html.index('<div class="composer-meta-left">'):html.index('<div class="composer-meta-right">')]
        self.assertIn('id="deepReasoningButton"', meta_left)
        self.assertIn('id="reasoningLabel"', meta_left)
        self.assertIn('id="reasoningValue"', meta_left)
        # 全视口动态背景层与全局毛玻璃弹窗规则存在
        self.assertIn('id="appBackdrop"', html)
        self.assertIn('dialog:not(.toast)', css)
        self.assertIn('html:is([data-chat-bg="on"], [data-model-skin="on"]) .sidebar', css)


if __name__ == "__main__":
    unittest.main()
