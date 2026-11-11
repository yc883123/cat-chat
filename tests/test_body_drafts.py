"""Execute local body storage and real workbench editing paths without a browser."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BodyDraftTests(unittest.TestCase):
    def test_storage_ordering_and_conflicts(self):
        self._run("body_draft_offline_test.cjs", "body draft offline: 12 cases passed")

    def test_workbench_switches_and_save_time_edits(self):
        self._run("body_draft_workbench_test.cjs", "body draft workbench: 8 cases passed")

    def _run(self, filename, expected):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for executable frontend tests")
        result = subprocess.run([node, str(ROOT / "verify" / filename)], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(expected, result.stdout)
