# -*- coding: utf-8 -*-
"""护栏：输入框 @ / Skill 弹层挂 body 后按视口定位（壁纸/模型皮肤下 backdrop-filter 不再劫持）。"""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend execution")
class ComposerPopupLayerTests(unittest.TestCase):
    def test_composer_popups_attach_to_body_before_positioning(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests" / "composer_popup_layer.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("composer popup layer:", result.stdout)
        self.assertIn("cases passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
