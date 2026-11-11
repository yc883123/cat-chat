"""Run real frontend navigation/model functions with deterministic HTTP gates."""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend execution")
class ConversationInflightTests(unittest.TestCase):
    def test_navigation_model_and_run_interleavings(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "verify" / "conversation_navigation_inflight_test.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("conversation navigation inflight:", result.stdout)
        self.assertIn("cases passed", result.stdout)
