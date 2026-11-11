"""Deterministic execution of frontend conversation navigation races."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ConversationRaceTests(unittest.TestCase):
    def test_model_selection_interleavings(self):
        result = subprocess.run(
            ["node", str(ROOT / "verify/model_selection_race_offline_test.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_navigation_interleavings(self):
        result = subprocess.run(
            ["node", str(ROOT / "verify/conversation_race_offline_test.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
